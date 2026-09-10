"""Restart re-spawn bug fix (#804 note 132791): the task->lane map is persisted
in the ClusterStore SQLite and reconciled on executor start.

If the executor dies mid-task and restarts, the previously-spawned task must
be RESUMED (tracked against its existing lane) rather than spawning a second
worker. The record (task_id, mode, jobId|pid, started_at) lives in the
``task_spawns`` table.

Mutation: remove the ``_persisted_spawn_task_ids()`` reconcile/dedup guard and
``test_restart_zero_respawns`` goes RED (the restarted executor re-spawns the
mid-task lane).
"""

import time
import os
from pathlib import Path
from unittest.mock import patch

from hermes_cluster.core.agent_executor import AgentExecutor, AgentExecutorConfig
from hermes_cluster.state.cluster_store import ClusterStore
from hermes_cluster.state import ClusterState


def _store(tmp_path=None):
    if tmp_path:
        return ClusterStore(db_path=str(tmp_path / "spawns.db"))
    return ClusterStore(db_path=":memory:")


def _executor_with_store(store, worker="bdaya-dispatch", **cfg_overrides):
    defaults = dict(enabled=True, poll_interval=60)
    defaults.update(cfg_overrides)
    return AgentExecutor(
        config=AgentExecutorConfig(**defaults, worker=worker),
        node_id="my-node",
        cluster_endpoint="http://127.0.0.1:9999",
        store=store,
    )


def _record(store, task_id, pid=4242, mode="bdaya-dispatch", started_at=None, lane_name=""):
    store.record_task_spawn(
        task_id=task_id,
        mode=mode,
        job_id=lane_name or f"hermes-{task_id}",
        pid=pid,
        started_at=started_at or time.time(),
        lane_name=lane_name or f"hermes-{task_id}",
    )


# ---------------------------------------------------------------------------
# ClusterStore persistence
# ---------------------------------------------------------------------------

class TestSpawnRecordStore:
    def test_record_get_delete(self):
        store = _store()
        _record(store, "task_a")
        row = store.get_task_spawn("task_a")
        assert row is not None
        assert row["task_id"] == "task_a"
        assert row["mode"] == "bdaya-dispatch"
        assert row["pid"] == 4242
        assert store.get_all_task_spawns()  # at least one row
        assert store.delete_task_spawn("task_a")
        assert store.get_task_spawn("task_a") is None

    def test_record_upserts_same_task(self):
        store = _store()
        _record(store, "task_a", pid=1)
        _record(store, "task_a", pid=2)
        assert len(store.get_all_task_spawns()) == 1
        assert store.get_task_spawn("task_a")["pid"] == 2

    def test_rows_survive_store_restart(self, tmp_path):
        db = str(tmp_path / "c.db")
        s1 = ClusterStore(db_path=db)
        _record(s1, "task_keep", pid=99)
        s1.close()
        s2 = ClusterStore(db_path=db)
        assert s2.get_task_spawn("task_keep")["pid"] == 99
        s2.close()


# ---------------------------------------------------------------------------
# Reconcile-on-start: spawn suppress replay (both store flavours)
# ---------------------------------------------------------------------------

class TestRestartReconcile:
    def test_restart_zero_respawns_for_existing_record(self):
        """A task with a persisted spawn record must NOT be re-spawned on restart."""
        store = _store()
        _record(store, "t_mid")
        executor = _executor_with_store(store)

        # After restart the executor finds the mid-task lane and resumes tracking it.
        executor._reconcile_persisted_spawns()
        assert "t_mid" in executor._active_spawns
        assert executor._active_spawns["t_mid"].resumed is True

        # Main still lists the task as running+assigned to this node.
        mock_tasks = [
            {"id": "t_mid", "title": "mid task", "status": "running",
             "assigned_to": "my-node", "priority": 3},
        ]
        with patch("hermes_cluster.core.agent_executor._signed_request", return_value=mock_tasks):
            with patch.object(executor, "_spawn_worker") as mock_spawn:
                executor._claim_and_spawn(max_spawns=5)
                mock_spawn.assert_not_called()  # zero spawns

    def test_restart_spawns_brand_new_task(self):
        """A task with NO record gets spawned normally on restart."""
        store = _store()
        executor = _executor_with_store(store)
        executor._reconcile_persisted_spawns()

        mock_tasks = [
            {"id": "t_new", "title": "new task", "status": "running",
             "assigned_to": "my-node", "priority": 2},
        ]
        with patch("hermes_cluster.core.agent_executor._signed_request", return_value=mock_tasks):
            with patch.object(executor, "_spawn_worker") as mock_spawn:
                executor._claim_and_spawn(max_spawns=5)
                mock_spawn.assert_called_once()
                assert mock_spawn.call_args[0][0]["id"] == "t_new"

    def test_restart_zero_respawns_hermes_mode(self):
        """Same guarantee in hermes worker mode (record carries mode+result_path)."""
        store = _store()
        _record(store, "t_hmid", mode="hermes", pid=777)
        executor = _executor_with_store(store, worker="hermes")
        executor._reconcile_persisted_spawns()
        assert executor._active_spawns["t_hmid"].mode == "hermes"

        mock_tasks = [
            {"id": "t_hmid", "title": "mid hermes", "status": "running",
             "assigned_to": "my-node", "priority": 3},
        ]
        with patch("hermes_cluster.core.agent_executor._signed_request", return_value=mock_tasks):
            with patch.object(executor, "_spawn_hermes_worker") as mock_spawn:
                executor._claim_and_spawn(max_spawns=5)
                mock_spawn.assert_not_called()

    def test_reconcile_runs_on_start(self, tmp_path):
        store = ClusterStore(db_path=str(tmp_path / "s.db"))
        _record(store, "t_start", pid=55)
        executor = _executor_with_store(store)
        executor.start()
        try:
            assert "t_start" in executor._active_spawns
        finally:
            executor.stop()
            store.close()

    def test_terminal_reap_drops_record(self, tmp_path):
        """Reaping a resumed lane to done must drop its persisted record."""
        store = ClusterStore(db_path=str(tmp_path / "r.db"))
        _record(store, "t_term", pid=123)
        executor = _executor_with_store(store)
        executor._reconcile_persisted_spawns()
        spawn = executor._active_spawns["t_term"]

        # Lane state reports done (bdaya mode) → reap completes and record drops.
        with patch.object(executor, "_query_all_lane_statuses",
                          return_value={"hermes-t_term": "done"}):
            with patch.object(executor, "_capture_spawn_exits"):
                with patch.object(executor, "_report_completion"):
                    executor._reap_finished_spawns()

        assert "t_term" not in executor._active_spawns
        assert store.get_task_spawn("t_term") is None
        store.close()


# ---------------------------------------------------------------------------
# In-memory ClusterState parity (drop-in for the same API)
# ---------------------------------------------------------------------------

class TestClusterStateParity:
    def test_cluster_state_exposes_same_spawn_api(self):
        state = ClusterState()
        _record(state, "t_state", pid=9)
        assert state.get_task_spawn("t_state")["pid"] == 9
        assert len(state.get_all_task_spawns()) == 1
        assert state.delete_task_spawn("t_state")
        assert state.get_task_spawn("t_state") is None