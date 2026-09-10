"""Tests for STATEFUL LANES in AgentExecutor (owner ruling 2026-09-10,
shared/claude-plugins#847/#833): a task carrying ``lane_key`` joins a named
stateful lane. The first task with a new ``lane_key`` spawns one hermes
session (titled by the lane_key); every later task with the same lane_key
RESUMES that live session (``--resume <session_id>``) and delivers its brief
as the next message. Worker restarts re-attach by lane_key.

Covers:
  - cluster API: SubmitTaskRequest/Task accept lane_key + role (author|reviewer)
  - same lane_key twice -> ONE session, TWO deliveries (no duplicate session)
  - restart mid-lane -> zero spawns, re-attach by lane_key
  - reviewer role -> opus-tier model (-m qwen3.7-plus) in the hermes argv
  - author role -> NO -m (profile default)
  - session_id captured from stderr and persisted into the lanes table
  - F1: timeout kills the orphaned child (no process leak)
"""

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from hermes_cluster.core.agent_executor import (
    ActiveSpawn,
    AgentExecutor,
    AgentExecutorConfig,
)
from hermes_cluster.models import SubmitTaskRequest, Task
from hermes_cluster.state import ClusterState
from hermes_cluster.state.cluster_store import ClusterStore


def _store(tmp_path=None):
    if tmp_path:
        return ClusterStore(db_path=str(tmp_path / "lanes.db"))
    return ClusterStore(db_path=":memory:")


def _executor_with_store(store, worker="hermes", **cfg_overrides):
    defaults = dict(enabled=True, poll_interval=60)
    defaults.update(cfg_overrides)
    return AgentExecutor(
        config=AgentExecutorConfig(**defaults, worker=worker),
        node_id="my-node",
        cluster_endpoint="http://127.0.0.1:9999",
        store=store,
        peer_token="test-token",
    )


class _FakeProc:
    pid = 777

    def __init__(self, results=None):
        self._results = list(results or [])
        self.stderr = None
        self.stdout = None

    def poll(self):
        return self._results.pop(0) if self._results else None


# ---------------------------------------------------------------------------
# Cluster API: lane_key + role on tasks
# ---------------------------------------------------------------------------

class TestClusterApiLaneFields:
    def test_submit_task_request_accepts_lane_key_and_role(self):
        req = SubmitTaskRequest(
            title="review the MR",
            lane_key="shared/claude-plugins#feat/x",
            role="reviewer",
        )
        assert req.lane_key == "shared/claude-plugins#feat/x"
        assert req.role == "reviewer"

    def test_submit_task_request_defaults(self):
        req = SubmitTaskRequest(title="plain task")
        assert req.lane_key == ""
        assert req.role == "author"

    def test_task_model_carries_lane_fields(self):
        task = Task(
            id="task_1",
            title="t",
            lane_key="shared/claude-plugins!895",
            role="reviewer",
        )
        assert task.lane_key == "shared/claude-plugins!895"
        assert task.role == "reviewer"

    def test_store_persists_lane_fields(self, tmp_path):
        store = ClusterStore(db_path=str(tmp_path / "t.db"))
        try:
            store.create_task(
                "task_l",
                "lane task",
                [],
                lane_key="shared/claude-plugins#feat/x",
                role="reviewer",
            )
            task = store.get_task("task_l")
            assert task is not None
            assert task.lane_key == "shared/claude-plugins#feat/x"
            assert task.role == "reviewer"
        finally:
            store.close()

    def test_in_memory_state_persists_lane_fields(self):
        state = ClusterState()
        state.create_task(
            "task_l2",
            "lane task 2",
            [],
            lane_key="other/lane",
            role="author",
        )
        task = state.get_task("task_l2")
        assert task.lane_key == "other/lane"
        assert task.role == "author"


# ---------------------------------------------------------------------------
# Lanes table (ClusterStore + ClusterState parity)
# ---------------------------------------------------------------------------

class TestLanesTable:
    def test_record_get_lane(self):
        store = _store()
        store.record_lane("L", session_id="sid_1", role="author", node="n1",
                         last_task_id="t1")
        lane = store.get_lane("L")
        assert lane is not None
        assert lane["session_id"] == "sid_1"
        assert lane["role"] == "author"
        assert lane["node"] == "n1"
        assert lane["last_task_id"] == "t1"

    def test_record_lane_keeps_created_at_on_upsert(self):
        store = _store()
        store.record_lane("L", session_id="sid_1", created_at=111.0)
        store.record_lane("L", session_id="sid_2", created_at=222.0)
        lane = store.get_lane("L")
        assert lane["created_at"] == 111.0  # first created_at wins
        assert lane["session_id"] == "sid_2"  # new session wins

    def test_touch_and_delete(self):
        store = _store()
        store.record_lane("L", session_id="s")
        store.touch_lane_last_task("L", "t_99")
        assert store.get_lane("L")["last_task_id"] == "t_99"
        assert store.delete_lane("L")
        assert store.get_lane("L") is None


# ---------------------------------------------------------------------------
# Same lane_key twice -> ONE session, TWO deliveries
# ---------------------------------------------------------------------------

class TestSameLaneKeyTwoDeliveries:
    def test_two_deliveries_one_session(self, monkeypatch, tmp_path):
        """First delivery spawns the lane (create-if-missing); the second
        RESUMES the lane's captured session (--resume <session_id>), and only
        ONE lane row + ONE hermes session id ever exists."""
        captured = []

        def fake_popen(cmd, **kw):
            captured.append(list(cmd))
            return _FakeProc()

        monkeypatch.setattr("hermes_cluster.core.agent_executor.subprocess.Popen", fake_popen)

        store = _store()
        executor = _executor_with_store(store, working_dir=str(tmp_path))

        task_a = {"id": "ta", "title": "first", "description": "d1",
                  "lane_key": "shared/claude-plugins#feat/x", "role": "author"}
        task_b = {"id": "tb", "title": "second", "description": "d2",
                  "lane_key": "shared/claude-plugins#feat/x", "role": "author"}

        # Delivery 1: new lane -> spawn with -c <lane_key> --create-if-missing
        executor._spawn_hermes_worker(task_a)
        spawn_a = executor._active_spawns["ta"]
        assert not spawn_a.resumed
        cmd_a = captured[0]
        assert "-c" in cmd_a
        assert cmd_a[cmd_a.index("-c") + 1] == "shared/claude-plugins#feat/x"
        assert "--create-if-missing" in cmd_a
        assert "--resume" not in cmd_a

        # Simulate: child writes its final response to the result file, exits,
        # and the stderr log carries hermes' session_id line.
        result_a = tmp_path / "hermes-results" / "ta.result.md"
        result_a.parent.mkdir(parents=True, exist_ok=True)
        result_a.write_text("the answer for delivery one", encoding="utf-8")
        stderr_a = tmp_path / "hermes-results" / "ta.stderr.log"
        stderr_a.parent.mkdir(parents=True, exist_ok=True)
        stderr_a.write_text("\nsession_id: 20260910_120000_abc\n", encoding="utf-8")
        spawn_a.stderr_path = str(stderr_a)
        spawn_a.process._results = [0]
        # Full reap path: capture session_id into the lanes table AND drop the
        # terminal task_spawn record (lanes row survives).
        with patch.object(executor, "_capture_spawn_exits"):
            with patch.object(executor, "_report_completion"):
                executor._reap_finished_spawns()
        # The lane's session id is now captured into the lanes table.
        lane = store.get_lane("shared/claude-plugins#feat/x")
        assert lane is not None
        assert lane["session_id"] == "20260910_120000_abc"
        # Terminal delivery drops the task_spawn record but KEEPS the lane row.
        assert store.get_task_spawn("ta") is None
        assert store.get_lane("shared/claude-plugins#feat/x") is not None

        # Delivery 2: same lane_key -> RESUMES the captued session.
        executor._spawn_hermes_worker(task_b)
        spawn_b = executor._active_spawns["tb"]
        assert spawn_b.resumed is True
        cmd_b = captured[1]
        assert "--resume" in cmd_b
        assert cmd_b[cmd_b.index("--resume") + 1] == "20260910_120000_abc"
        assert "-c" not in cmd_b  # no create-if-missing on resume
        assert "--create-if-missing" not in cmd_b

        # Exactly ONE lane row survives both deliveries.
        lanes = store.get_all_lanes()
        assert len(lanes) == 1
        assert lanes[0]["lane_key"] == "shared/claude-plugins#feat/x"
        assert lanes[0]["last_task_id"] == "tb"

    def test_deliveries_share_session_id_asserted(self, monkeypatch, tmp_path):
        """The same session_id is used across deliveries -> one live session."""
        store = _store()
        store.record_lane("L", session_id="live_sid_42", last_task_id="t_old")
        captured = {}

        class _P:
            pid = 9
            stderr = None
            stdout = None
            def poll(self): return None

        monkeypatch.setattr("hermes_cluster.core.agent_executor.subprocess.Popen",
                            lambda cmd, **kw: captured.__setitem__("cmd", cmd) or _P())
        executor = _executor_with_store(store, working_dir=str(tmp_path))
        executor._spawn_hermes_worker(
            {"id": "t_next", "title": "next", "lane_key": "L", "role": "author"}
        )
        cmd = captured["cmd"]
        assert "--resume" in cmd
        assert cmd[cmd.index("--resume") + 1] == "live_sid_42"
        spawn = executor._active_spawns["t_next"]
        assert spawn.session_id == "live_sid_42"


# ---------------------------------------------------------------------------
# Role -> model mapping
# ---------------------------------------------------------------------------

class TestRoleToModel:
    def test_reviewer_role_passes_plus_model_in_argv(self, monkeypatch, tmp_path):
        captured = {}

        class _P:
            pid = 5
            stderr = None
            stdout = None
            def poll(self): return None

        monkeypatch.setattr("hermes_cluster.core.agent_executor.subprocess.Popen",
                            lambda cmd, **kw: captured.__setitem__("cmd", cmd) or _P())
        executor = _executor_with_store(_store(), working_dir=str(tmp_path))
        executor._spawn_hermes_worker(
            {"id": "trev", "title": "review", "role": "reviewer",
             "lane_key": "shared/claude-plugins!895"}
        )
        cmd = captured["cmd"]
        assert "-m" in cmd
        assert cmd[cmd.index("-m") + 1] == "qwen3.7-plus"

    def test_author_role_no_model_flag(self, monkeypatch, tmp_path):
        captured = {}

        class _P:
            pid = 6
            stderr = None
            stdout = None
            def poll(self): return None

        monkeypatch.setattr("hermes_cluster.core.agent_executor.subprocess.Popen",
                            lambda cmd, **kw: captured.__setitem__("cmd", cmd) or _P())
        executor = _executor_with_store(_store(), working_dir=str(tmp_path))
        executor._spawn_hermes_worker(
            {"id": "tauth", "title": "write", "role": "author",
             "lane_key": "shared/claude-plugins#feat/x"}
        )
        cmd = captured["cmd"]
        assert "-m" not in cmd  # author uses the profile default (qwen3.8-flash)

    def test_reviewer_model_configurable(self, monkeypatch, tmp_path):
        captured = {}

        class _P:
            pid = 7
            stderr = None
            stdout = None
            def poll(self): return None

        monkeypatch.setattr("hermes_cluster.core.agent_executor.subprocess.Popen",
                            lambda cmd, **kw: captured.__setitem__("cmd", cmd) or _P())
        executor = _executor_with_store(
            _store(), working_dir=str(tmp_path),
            hermes_reviewer_model="qwen3.7-plus",
        )
        executor._spawn_hermes_worker(
            {"id": "tr2", "title": "review2", "role": "reviewer", "lane_key": "L"}
        )
        assert "-m" in captured["cmd"]


# ---------------------------------------------------------------------------
# Restart mid-lane -> zero spawns, re-attach by lane_key
# ---------------------------------------------------------------------------

class TestRestartReattachByLaneKey:
    def test_restart_mid_lane_zero_spawns(self, tmp_path):
        """Store has a live lane + a persisted spawn for the in-flight task;
        the restarted executor reattaches by lane_key and spawns NOTHING."""
        store = ClusterStore(db_path=str(tmp_path / "r.db"))
        try:
            store.record_lane("lane/restart", session_id="sid_restart",
                              role="author", last_task_id="t_mid")
            store.record_task_spawn(
                task_id="t_mid", mode="hermes", pid=4321,
                lane_key="lane/restart", role="author",
                session_id="sid_restart",
                result_path=str(tmp_path / "hermes-results" / "t_mid.result.md"),
            )
            executor = _executor_with_store(store)
            executor._reconcile_persisted_spawns()

            # The mid-lane task is re-attached, NOT re-spawned.
            assert "t_mid" in executor._active_spawns
            spawn = executor._active_spawns["t_mid"]
            assert spawn.resumed is True
            assert spawn.session_id == "sid_restart"
            assert spawn.lane_key == "lane/restart"

            mock_tasks = [
                {"id": "t_mid", "title": "mid", "status": "running",
                 "assigned_to": "my-node", "priority": 3,
                 "lane_key": "lane/restart", "role": "author"},
            ]
            with patch("hermes_cluster.core.agent_executor._signed_request",
                       return_value=mock_tasks):
                with patch.object(executor, "_spawn_worker") as mock_spawn:
                    executor._claim_and_spawn(max_spawns=5)
                    mock_spawn.assert_not_called()  # zero spawns on restart
        finally:
            store.close()

    def test_restart_new_task_same_lane_resumes(self, monkeypatch, tmp_path):
        """After a restart the first NEW delivery on an existing lane resumes
        the lane's captured session rather than spawning a second session."""
        store = ClusterStore(db_path=str(tmp_path / "rr.db"))
        try:
            store.record_lane("lane/rr", session_id="sid_rr", last_task_id="old")
            captured = {}

            class _P:
                pid = 11
                stderr = None
                stdout = None
                def poll(self): return None

            monkeypatch.setattr(
                "hermes_cluster.core.agent_executor.subprocess.Popen",
                lambda cmd, **kw: captured.__setitem__("cmd", cmd) or _P())
            executor = _executor_with_store(store, working_dir=str(tmp_path))
            executor._spawn_hermes_worker(
                {"id": "t_aft", "title": "after restart", "role": "author",
                 "lane_key": "lane/rr"}
            )
            cmd = captured["cmd"]
            assert "--resume" in cmd
            assert cmd[cmd.index("--resume") + 1] == "sid_rr"
            assert "--create-if-missing" not in cmd
        finally:
            store.close()


# ---------------------------------------------------------------------------
# F1: timeout kills the orphaned child
# ---------------------------------------------------------------------------

class TestTimeoutKillsSpawn:
    def test_timeout_kills_live_child(self, tmp_path):
        store = _store()
        executor = _executor_with_store(store, spawn_timeout=10.0,
                                        working_dir=str(tmp_path))
        proc = _FakeProc()
        spawn = ActiveSpawn(
            task_id="t_kill", task_title="kill me",
            process=proc, started_at=time.time() - 30,
            lane_name="hermes-t_kill", mode="hermes",
            result_path=str(tmp_path / "r.md"),
            lane_key="L", role="author",
        )
        with patch.object(executor, "_kill_spawn_process") as mock_kill:
            resolved = []
            executor._reap_hermes_spawn("t_kill", spawn, 30.0, resolved)
            assert resolved and resolved[0][2] == "timeout"
            mock_kill.assert_called_once_with(spawn)

    def test_kill_skips_resumed_process(self):
        store = _store()
        executor = _executor_with_store(store)
        spawn = ActiveSpawn(
            task_id="t_res", task_title="resumed",
            process=_FakeProc(), started_at=time.time(),
            lane_name="hermes-t_res", mode="hermes", resumed=True,
        )
        executor._kill_spawn_process(spawn)  # must not raise