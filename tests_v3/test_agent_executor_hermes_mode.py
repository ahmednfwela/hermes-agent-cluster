"""Tests for the NATIVE hermes worker mode in AgentExecutor.

Config ``agent_executor.worker: "hermes"`` spawns a non-interactive hermes
session (``hermes -p <profile> chat --query-file <brief> -Q``) and tracks the
process by pid + exit code + result file — instead of bdaya-dispatch lane
status. C14 (shared/claude-plugins#847-adjacent).

Covers:
  - config default worker stays bdaya-dispatch
  - hermes mode spawn builds the documented invocation
  - hermes mode reaps by result file + exit code (done / fail / no-result / timeout)
  - spawned hermes lane writes result file to hermes-results/<task>.result.md
"""

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from hermes_cluster.core.agent_executor import (
    ActiveSpawn,
    AgentExecutor,
    AgentExecutorConfig,
)


def _executor(**cfg_overrides) -> AgentExecutor:
    defaults = dict(enabled=True, poll_interval=60)
    defaults.update(cfg_overrides)
    return AgentExecutor(
        config=AgentExecutorConfig(**defaults),
        node_id="test-node",
        cluster_endpoint="http://127.0.0.1:9999",
    )


def _hermes_spawn(task_id: str, started_at: float = None, **kw) -> ActiveSpawn:
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # still running
    mock_proc.pid = 4242
    mock_proc.stderr = None
    mock_proc.stdout = None
    return ActiveSpawn(
        task_id=task_id,
        task_title=f"task {task_id}",
        process=mock_proc,
        lease_id=f"lease_{task_id}",
        started_at=started_at or time.time(),
        lane_name=f"hermes-{task_id}",
        mode="hermes",
        result_path=str(kw.pop("result_path", "") or f"/tmp/{task_id}.result.md"),
        **kw,
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestHermesConfig:
    def test_default_worker_is_bdaya_dispatch(self):
        assert AgentExecutorConfig().worker == "bdaya-dispatch"

    def test_hermes_mode_values(self):
        cfg = AgentExecutorConfig(worker="hermes", hermes_profile="bdaya", hermes_bin="/custom/hermes")
        assert cfg.worker == "hermes"
        assert cfg.hermes_profile == "bdaya"
        assert cfg.hermes_bin == "/custom/hermes"


# ---------------------------------------------------------------------------
# Spawn
# ---------------------------------------------------------------------------

class TestHermesSpawn:
    def test_spawn_hermes_uses_query_file_quiet_and_profile(self, monkeypatch, tmp_path):
        """The native invocation is the documented one:
        ``hermes -p <profile> chat --query-file <brief> -Q``."""
        captured = {}

        class _FakeProc:
            pid = 777
            stderr = None
            stdout = None
            def poll(self):
                return None

        def fake_popen(cmd, **kw):
            captured["cmd"] = cmd
            return _FakeProc()

        monkeypatch.setattr("hermes_cluster.core.agent_executor.subprocess.Popen", fake_popen)

        executor = _executor(
            worker="hermes",
            hermes_profile="default",
            hermes_bin="/usr/local/bin/hermes",
            working_dir=str(tmp_path),
        )
        executor._spawn_hermes_worker({"id": "task_h1", "title": "do hermes thing", "description": "desc"})

        cmd = captured["cmd"]
        assert cmd[0] == "/usr/local/bin/hermes"
        assert cmd[cmd.index("-p") + 1] == "default"
        assert "chat" in cmd
        assert "-Q" in cmd
        assert "--query-file" in cmd
        brief = Path(cmd[cmd.index("--query-file") + 1])
        assert brief.exists()
        assert "do hermes thing" in brief.read_text(encoding="utf-8")

    def test_spawn_hermes_persists_result_path_and_maps_spawn(self, monkeypatch, tmp_path):
        captured = {}

        class _FakeProc:
            pid = 778
            stderr = None
            stdout = None
            def poll(self):
                return None

        def fake_popen(cmd, **kw):
            captured["cmd"] = cmd
            return _FakeProc()

        monkeypatch.setattr("hermes_cluster.core.agent_executor.subprocess.Popen", fake_popen)

        executor = _executor(
            worker="hermes",
            working_dir=str(tmp_path),
        )
        executor._spawn_hermes_worker({"id": "task_h2", "title": "t"})

        spawn = executor._active_spawns["task_h2"]
        assert spawn.mode == "hermes"
        assert spawn.process.pid == 778
        result = Path(spawn.result_path)
        assert result.parent == tmp_path / "hermes-results"

    def test_spawn_hermes_missing_bin_reports_failure(self, monkeypatch, tmp_path):
        def missing_popen(cmd, **kw):
            raise FileNotFoundError("hermes")

        monkeypatch.setattr("hermes_cluster.core.agent_executor.subprocess.Popen", missing_popen)
        executor = _executor(worker="hermes", working_dir=str(tmp_path))
        with patch.object(executor, "_report_failure") as mock_fail:
            executor._spawn_hermes_worker({"id": "task_h3", "title": "t"})
            mock_fail.assert_called_once()
            assert "hermes" in mock_fail.call_args[0][1].lower()


# ---------------------------------------------------------------------------
# Reaping
# ---------------------------------------------------------------------------

class TestHermesReap:
    def test_done_when_exit_zero_and_result_written(self, tmp_path):
        executor = _executor(worker="hermes")
        result_path = tmp_path / "r.md"
        result_path.write_text("here is the answer", encoding="utf-8")
        spawn = _hermes_spawn("t_done", result_path=str(result_path))
        spawn.process.poll.return_value = 0
        executor._active_spawns["t_done"] = spawn

        with patch.object(executor, "_capture_spawn_exits"):
            resolved = []
            executor._reap_hermes_spawn("t_done", spawn, 12.0, resolved)

        assert len(resolved) == 1
        task_id, _spawn, outcome, detail = resolved[0]
        assert outcome == "done"
        assert str(result_path) in detail

    def test_fail_when_exit_nonzero(self, tmp_path):
        executor = _executor(worker="hermes")
        result_path = tmp_path / "empty.md"
        result_path.write_text("", encoding="utf-8")
        spawn = _hermes_spawn("t_fail", result_path=str(result_path))
        spawn.process.poll.return_value = 3
        executor._active_spawns["t_fail"] = spawn

        resolved = []
        executor._reap_hermes_spawn("t_fail", spawn, 5.0, resolved)

        assert resolved and resolved[0][2] == "spawn_failed"
        assert "rc=3" in resolved[0][3]

    def test_no_result_when_exit_zero_but_empty_file(self, tmp_path):
        executor = _executor(worker="hermes")
        result_path = tmp_path / "blank.md"
        result_path.write_text("   \n", encoding="utf-8")
        spawn = _hermes_spawn("t_blank", result_path=str(result_path))
        spawn.process.poll.return_value = 0
        executor._active_spawns["t_blank"] = spawn

        resolved = []
        executor._reap_hermes_spawn("t_blank", spawn, 3.0, resolved)

        assert resolved and resolved[0][2] == "no_result"

    def test_timeout_when_still_running(self, tmp_path):
        executor = _executor(worker="hermes", spawn_timeout=10.0)
        result_path = tmp_path / "run.md"
        result_path.write_text("", encoding="utf-8")
        spawn = _hermes_spawn(
            "t_run",
            started_at=time.time() - 20.0,  # exceeds 10s timeout
            result_path=str(result_path),
        )
        executor._active_spawns["t_run"] = spawn

        resolved = []
        executor._reap_hermes_spawn("t_run", spawn, 20.0, resolved)

        assert resolved and resolved[0][2] == "timeout"

    def test_keeps_waiting_while_alive_within_timeout(self, tmp_path):
        executor = _executor(worker="hermes", spawn_timeout=60.0)
        result_path = tmp_path / "w.md"
        result_path.write_text("", encoding="utf-8")
        spawn = _hermes_spawn("t_wait", result_path=str(result_path))
        spawn.process.poll.return_value = None
        executor._active_spawns["t_wait"] = spawn

        resolved = []
        executor._reap_hermes_spawn("t_wait", spawn, 5.0, resolved)

        assert resolved == []

    def test_reap_finished_skips_lane_status_for_hermes(self, tmp_path):
        """Hermes-mode spawns must NOT be reaped through bdaya-dispatch status."""
        executor = _executor(worker="hermes")
        result_path = tmp_path / "done2.md"
        result_path.write_text("answer", encoding="utf-8")
        spawn = _hermes_spawn("t_h5", result_path=str(result_path))
        spawn.process.poll.return_value = 0
        executor._active_spawns["t_h5"] = spawn

        with patch.object(executor, "_query_all_lane_statuses") as mock_query:
            with patch.object(executor, "_report_completion") as mock_done:
                executor._reap_finished_spawns()
                mock_query.assert_not_called()
                mock_done.assert_called_once_with("t_h5", detail=mock_done.call_args[1]["detail"])

        assert "t_h5" not in executor._active_spawns