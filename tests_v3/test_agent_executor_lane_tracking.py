"""Tests for lane-status-based completion tracking in AgentExecutor.

The executor no longer relies on subprocess exit codes (bdaya-dispatch run
backgrounds the lane and exits 0 immediately). Instead it polls
``bdaya-dispatch status --json`` and inspects the lane state.

Covers:
  - done lane -> _report_completion called
  - blocked lane -> _report_failure called
  - stopped/failed lanes -> _report_failure called
  - missing lane (not in status output) -> _report_failure called
  - spawn_timeout exceeded -> _report_failure called
  - _query_all_lane_statuses parses JSON correctly
  - _query_all_lane_statuses handles errors gracefully
"""

import json
import time
import pytest
from unittest.mock import patch, MagicMock

from hermes_cluster.core.agent_executor import AgentExecutor, AgentExecutorConfig, ActiveSpawn


def _make_executor(**cfg_overrides) -> AgentExecutor:
    defaults = dict(enabled=True, poll_interval=60)
    defaults.update(cfg_overrides)
    return AgentExecutor(
        config=AgentExecutorConfig(**defaults),
        node_id="test-node",
        cluster_endpoint="http://127.0.0.1:9999",
    )


def _make_spawn(task_id: str, lane_name: str = "", started_at: float = None, **kw) -> ActiveSpawn:
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # process "still running" (backgrounded)
    mock_proc.pid = 12345
    return ActiveSpawn(
        task_id=task_id,
        task_title=f"task {task_id}",
        process=mock_proc,
        lease_id=f"lease_{task_id}",
        started_at=started_at or time.time(),
        lane_name=lane_name or f"hermes-{task_id}",
        **kw,
    )


# ---------------------------------------------------------------------------
# Lane status query
# ---------------------------------------------------------------------------

class TestQueryAllLaneStatuses:
    def test_parses_json_correctly(self):
        """Parse the status JSON and return {lane_name: state}."""
        executor = _make_executor()
        fake_output = json.dumps({
            "lanes": [
                {"lane": "hermes-t1", "state": "working"},
                {"lane": "hermes-t2", "state": "done"},
                {"lane": "hermes-t3", "state": "blocked"},
            ]
        })
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = fake_output
        mock_result.stderr = ""

        with patch("hermes_cluster.core.agent_executor.subprocess.run", return_value=mock_result):
            statuses = executor._query_all_lane_statuses()

        assert statuses == {
            "hermes-t1": "working",
            "hermes-t2": "done",
            "hermes-t3": "blocked",
        }

    def test_returns_empty_on_nonzero_exit(self):
        executor = _make_executor()
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "some error"

        with patch("hermes_cluster.core.agent_executor.subprocess.run", return_value=mock_result):
            assert executor._query_all_lane_statuses() == {}

    def test_returns_empty_on_invalid_json(self):
        executor = _make_executor()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not json"

        with patch("hermes_cluster.core.agent_executor.subprocess.run", return_value=mock_result):
            assert executor._query_all_lane_statuses() == {}

    def test_returns_empty_on_timeout(self):
        import subprocess
        executor = _make_executor()
        with patch("hermes_cluster.core.agent_executor.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(cmd="npx", timeout=30)):
            assert executor._query_all_lane_statuses() == {}

    def test_returns_empty_on_npx_not_found(self):
        executor = _make_executor()
        with patch("hermes_cluster.core.agent_executor.subprocess.run",
                    side_effect=FileNotFoundError("npx")):
            assert executor._query_all_lane_statuses() == {}


# ---------------------------------------------------------------------------
# Reap finished spawns — lane-state-based
# ---------------------------------------------------------------------------

class TestReapFinishedSpawnsLaneTracking:
    def test_done_lane_triggers_completion(self):
        """A lane with state=done triggers _report_completion."""
        executor = _make_executor()
        spawn = _make_spawn("task_abc", "hermes-task_abc")
        executor._active_spawns["task_abc"] = spawn

        lane_statuses = {"hermes-task_abc": "done"}

        with patch.object(executor, "_query_all_lane_statuses", return_value=lane_statuses):
            with patch.object(executor, "_report_completion") as mock_complete:
                with patch.object(executor, "_report_failure") as mock_fail:
                    executor._reap_finished_spawns()
                    mock_complete.assert_called_once_with("task_abc")
                    mock_fail.assert_not_called()

        assert "task_abc" not in executor._active_spawns

    def test_blocked_lane_triggers_failure(self):
        """A lane with state=blocked triggers _report_failure."""
        executor = _make_executor()
        spawn = _make_spawn("task_blk", "hermes-task_blk")
        executor._active_spawns["task_blk"] = spawn

        lane_statuses = {"hermes-task_blk": "blocked"}

        with patch.object(executor, "_query_all_lane_statuses", return_value=lane_statuses):
            with patch.object(executor, "_report_completion") as mock_complete:
                with patch.object(executor, "_report_failure") as mock_fail:
                    executor._reap_finished_spawns()
                    mock_fail.assert_called_once()
                    mock_complete.assert_not_called()
                    reason = mock_fail.call_args[0][1]
                    assert "blocked" in reason
                    assert "hermes-task_blk" in reason

        assert "task_blk" not in executor._active_spawns

    def test_stopped_lane_triggers_failure(self):
        executor = _make_executor()
        spawn = _make_spawn("task_stp", "hermes-task_stp")
        executor._active_spawns["task_stp"] = spawn

        with patch.object(executor, "_query_all_lane_statuses",
                          return_value={"hermes-task_stp": "stopped"}):
            with patch.object(executor, "_report_failure") as mock_fail:
                executor._reap_finished_spawns()
                mock_fail.assert_called_once()
                assert "stopped" in mock_fail.call_args[0][1]

    def test_failed_lane_triggers_failure(self):
        executor = _make_executor()
        spawn = _make_spawn("task_fail", "hermes-task_fail")
        executor._active_spawns["task_fail"] = spawn

        with patch.object(executor, "_query_all_lane_statuses",
                          return_value={"hermes-task_fail": "failed"}):
            with patch.object(executor, "_report_failure") as mock_fail:
                executor._reap_finished_spawns()
                mock_fail.assert_called_once()
                assert "failed" in mock_fail.call_args[0][1]

    def test_missing_lane_triggers_failure(self):
        """A lane not present in status output triggers _report_failure."""
        executor = _make_executor()
        spawn = _make_spawn("task_miss", "hermes-task_miss")
        executor._active_spawns["task_miss"] = spawn

        # Lane not in the returned statuses at all
        with patch.object(executor, "_query_all_lane_statuses", return_value={}):
            with patch.object(executor, "_report_failure") as mock_fail:
                executor._reap_finished_spawns()
                mock_fail.assert_called_once()
                assert "not found" in mock_fail.call_args[0][1].lower() or "missing" in mock_fail.call_args[0][1].lower()

    def test_working_lane_not_reaped(self):
        """A lane with state=working is left active (not reaped)."""
        executor = _make_executor()
        spawn = _make_spawn("task_wip", "hermes-task_wip")
        executor._active_spawns["task_wip"] = spawn

        with patch.object(executor, "_query_all_lane_statuses",
                          return_value={"hermes-task_wip": "working"}):
            with patch.object(executor, "_report_completion") as mock_complete:
                with patch.object(executor, "_report_failure") as mock_fail:
                    executor._reap_finished_spawns()
                    mock_complete.assert_not_called()
                    mock_fail.assert_not_called()

        assert "task_wip" in executor._active_spawns

    def test_spawn_timeout_triggers_failure(self):
        """A spawn exceeding spawn_timeout is reported as failed."""
        executor = _make_executor(spawn_timeout=10.0)
        spawn = _make_spawn("task_to", "hermes-task_to",
                            started_at=time.time() - 20.0)  # 20s ago, timeout is 10s
        executor._active_spawns["task_to"] = spawn

        # Even though lane shows "working", timeout should win
        with patch.object(executor, "_query_all_lane_statuses",
                          return_value={"hermes-task_to": "working"}):
            with patch.object(executor, "_report_failure") as mock_fail:
                executor._reap_finished_spawns()
                mock_fail.assert_called_once()
                assert "timeout" in mock_fail.call_args[0][1].lower() or "exceeded" in mock_fail.call_args[0][1].lower()

        assert "task_to" not in executor._active_spawns

    def test_no_active_spawns_skips_query(self):
        """When no spawns are active, _query_all_lane_statuses is not called."""
        executor = _make_executor()

        with patch.object(executor, "_query_all_lane_statuses") as mock_query:
            executor._reap_finished_spawns()
            mock_query.assert_not_called()

    def test_multiple_spawns_mixed_states(self):
        """Multiple spawns with different states are handled correctly."""
        executor = _make_executor()
        executor._active_spawns["t1"] = _make_spawn("t1", "hermes-t1")
        executor._active_spawns["t2"] = _make_spawn("t2", "hermes-t2")
        executor._active_spawns["t3"] = _make_spawn("t3", "hermes-t3")
        executor._active_spawns["t4"] = _make_spawn("t4", "hermes-t4")

        lane_statuses = {
            "hermes-t1": "done",
            "hermes-t2": "working",
            "hermes-t3": "blocked",
            "hermes-t4": "failed",
        }

        with patch.object(executor, "_query_all_lane_statuses", return_value=lane_statuses):
            with patch.object(executor, "_report_completion") as mock_complete:
                with patch.object(executor, "_report_failure") as mock_fail:
                    executor._reap_finished_spawns()

                    # t1 done -> completion
                    mock_complete.assert_called_once_with("t1")

                    # t3 blocked, t4 failed -> failure
                    assert mock_fail.call_count == 2
                    fail_task_ids = {call[0][0] for call in mock_fail.call_args_list}
                    assert fail_task_ids == {"t3", "t4"}

        # t2 still working -> still active
        assert "t2" in executor._active_spawns
        assert "t1" not in executor._active_spawns
        assert "t3" not in executor._active_spawns
        assert "t4" not in executor._active_spawns
