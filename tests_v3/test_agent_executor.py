"""Tests for the agent executor and lease extend endpoint.

Covers:
  1. POST /api/v1/leases/{id}/extend — lease TTL renewal
  2. GET /api/v1/executor/status — executor diagnostics
  3. AgentExecutor core logic (unit tests, no real subprocess spawn)
"""

import time
import pytest
from unittest.mock import patch, MagicMock
from datetime import timedelta

from fastapi.testclient import TestClient

from hermes_cluster.app import create_app
from hermes_cluster.core.agent_executor import AgentExecutor, AgentExecutorConfig


@pytest.fixture
def app():
    """Create a test app with fresh state (main role, no executor)."""
    return create_app(
        cluster_id="test-cluster",
        node_id="test-node",
        node_role="main",
    )


@pytest.fixture
def client(app):
    return TestClient(app)


def _register_node(client, name="worker-1", capabilities=None):
    resp = client.post("/api/v1/nodes/join", json={
        "node_name": name,
        "capabilities": capabilities or ["tooling"],
    })
    return resp.json()["node_id"]


def _submit_task(client, title="Test task", requires=None, priority=0):
    resp = client.post("/api/v1/tasks", json={
        "title": title,
        "requires": requires or [],
        "priority": priority,
    })
    return resp.json()


def _claim_task(client, task_id, node_id):
    resp = client.post(f"/api/v1/tasks/{task_id}/claim", json={
        "node_id": node_id,
    })
    return resp.json()


# ---------------------------------------------------------------------------
# 1. Lease extend endpoint
# ---------------------------------------------------------------------------

class TestLeaseExtend:
    def test_extend_active_lease(self, client):
        """Extending an active lease returns a new lease with later expiry."""
        node_id = _register_node(client, "worker-ext")
        task = _submit_task(client, "Extend me")
        claimed = _claim_task(client, task["id"], node_id)

        # Find the lease
        leases = client.get("/api/v1/leases").json()
        active = [l for l in leases if l["task_id"] == task["id"] and l["status"] == "active"]
        assert len(active) == 1
        lease_id = active[0]["id"]

        # Extend
        resp = client.post(f"/api/v1/leases/{lease_id}/extend")
        assert resp.status_code == 200
        new_lease = resp.json()
        assert new_lease["status"] == "active"
        assert new_lease["task_id"] == task["id"]
        # New expiry should be later than original
        assert new_lease["expires_at"] > active[0]["expires_at"]

    def test_extend_nonexistent_lease(self, client):
        """Extending a nonexistent lease returns 404."""
        resp = client.post("/api/v1/leases/lease_nonexistent/extend")
        assert resp.status_code == 404

    def test_extend_revoked_lease(self, client):
        """Extending a revoked lease returns 404."""
        node_id = _register_node(client, "worker-rev")
        task = _submit_task(client, "Revoke then extend")
        _claim_task(client, task["id"], node_id)

        leases = client.get("/api/v1/leases").json()
        active = [l for l in leases if l["task_id"] == task["id"] and l["status"] == "active"]
        lease_id = active[0]["id"]

        # Revoke first
        client.delete(f"/api/v1/leases/{lease_id}")

        # Now try to extend — should fail
        resp = client.post(f"/api/v1/leases/{lease_id}/extend")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 2. Executor status endpoint
# ---------------------------------------------------------------------------

class TestExecutorStatus:
    def test_executor_not_running(self, client):
        """Main node has no executor — status reports disabled."""
        resp = client.get("/api/v1/executor/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False

    def test_executor_running(self):
        """Worker node with executor config shows enabled status."""
        app = create_app(
            cluster_id="test-cluster",
            node_id="test-worker",
            node_role="worker",
            cluster_endpoint="http://127.0.0.1:9999",  # unreachable, but that's OK
            agent_executor_config={
                "enabled": True,
                "profile": "alibaba1",
                "model": "sonnet",
                "poll_interval": 60,  # long interval so it doesn't poll during test
                "max_concurrent": 1,
            },
        )
        with TestClient(app) as tc:
            resp = tc.get("/api/v1/executor/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["enabled"] is True
            assert data["profile"] == "alibaba1"
            assert data["model"] == "sonnet"
            assert data["active_spawns"] == 0


# ---------------------------------------------------------------------------
# 3. AgentExecutor unit tests (no real subprocess)
# ---------------------------------------------------------------------------

class TestAgentExecutorUnit:
    def test_config_defaults(self):
        """Default config has sensible values."""
        cfg = AgentExecutorConfig()
        assert cfg.enabled is False
        assert cfg.profile == "alibaba1"
        assert cfg.model == "sonnet"
        assert cfg.poll_interval == 15.0
        assert cfg.max_concurrent == 1
        assert cfg.spawn_timeout == 1800.0

    def test_start_stop(self):
        """Executor starts and stops cleanly."""
        cfg = AgentExecutorConfig(enabled=True, poll_interval=60)
        executor = AgentExecutor(
            config=cfg,
            node_id="test-node",
            cluster_endpoint="http://127.0.0.1:9999",
        )
        executor.start()
        assert executor.is_running
        assert executor.active_count == 0
        executor.stop()
        assert not executor.is_running

    def test_status_returns_config(self):
        """Status includes config and spawn info."""
        cfg = AgentExecutorConfig(
            enabled=True,
            profile="test-profile",
            model="test-model",
            max_concurrent=2,
        )
        executor = AgentExecutor(
            config=cfg,
            node_id="test-node",
            cluster_endpoint="http://127.0.0.1:9999",
        )
        status = executor.status()
        assert status["running"] is False
        assert status["profile"] == "test-profile"
        assert status["model"] == "test-model"
        assert status["max_concurrent"] == 2
        assert status["active_spawns"] == 0
        assert status["spawns"] == []

    def test_reap_finished_spawn_success(self):
        """A spawn that exits 0 triggers completion report."""
        cfg = AgentExecutorConfig(enabled=True)
        executor = AgentExecutor(
            config=cfg,
            node_id="test-node",
            cluster_endpoint="http://127.0.0.1:9999",
        )

        # Mock a finished process (exit code 0)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.stdout = None
        mock_proc.pid = 12345

        from hermes_cluster.core.agent_executor import ActiveSpawn
        spawn = ActiveSpawn(
            task_id="task_abc",
            task_title="test task",
            process=mock_proc,
            lease_id="lease_123",
            started_at=time.time(),
            lane_name="hermes-task_abc",
        )
        executor._active_spawns["task_abc"] = spawn

        # Mock the report call
        with patch.object(executor, "_report_completion") as mock_complete:
            executor._reap_finished_spawns()
            mock_complete.assert_called_once_with("task_abc")

        # Spawn should be removed
        assert "task_abc" not in executor._active_spawns

    def test_reap_finished_spawn_failure(self):
        """A spawn that exits non-zero triggers failure report."""
        cfg = AgentExecutorConfig(enabled=True)
        executor = AgentExecutor(
            config=cfg,
            node_id="test-node",
            cluster_endpoint="http://127.0.0.1:9999",
        )

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1
        mock_proc.stdout = None
        mock_proc.pid = 12345

        from hermes_cluster.core.agent_executor import ActiveSpawn
        spawn = ActiveSpawn(
            task_id="task_def",
            task_title="failing task",
            process=mock_proc,
            lease_id="lease_456",
            started_at=time.time(),
            lane_name="hermes-task_def",
        )
        executor._active_spawns["task_def"] = spawn

        with patch.object(executor, "_report_failure") as mock_fail:
            executor._reap_finished_spawns()
            mock_fail.assert_called_once()
            call_args = mock_fail.call_args
            assert call_args[0][0] == "task_def"
            assert "exited with code 1" in call_args[0][1]

    def test_reap_timeout_spawn(self):
        """A spawn that exceeds timeout is killed and reported as failed."""
        cfg = AgentExecutorConfig(enabled=True, spawn_timeout=1.0)
        executor = AgentExecutor(
            config=cfg,
            node_id="test-node",
            cluster_endpoint="http://127.0.0.1:9999",
        )

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        mock_proc.pid = 12345
        mock_proc.kill = MagicMock()

        from hermes_cluster.core.agent_executor import ActiveSpawn
        spawn = ActiveSpawn(
            task_id="task_timeout",
            task_title="hung task",
            process=mock_proc,
            lease_id="lease_789",
            started_at=time.time() - 10.0,  # 10 seconds ago, timeout is 1s
            lane_name="hermes-task_timeout",
        )
        executor._active_spawns["task_timeout"] = spawn

        with patch.object(executor, "_report_failure") as mock_fail:
            executor._reap_finished_spawns()
            mock_proc.kill.assert_called_once()
            mock_fail.assert_called_once()

    def test_claim_and_spawn_dedup_no_double_spawn(self):
        """A task already in _active_spawns is NOT re-spawned on re-poll (B2).

        Mutation: remove the dedup guard (the `task_id not in active_task_ids`
        check in _claim_and_spawn) and this test goes RED.
        """
        cfg = AgentExecutorConfig(enabled=True, max_concurrent=3)
        executor = AgentExecutor(
            config=cfg,
            node_id="my-node",
            cluster_endpoint="http://127.0.0.1:9999",
        )

        # Simulate task "t1" already actively spawned
        from hermes_cluster.core.agent_executor import ActiveSpawn
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        mock_proc.pid = 99999
        executor._active_spawns["t1"] = ActiveSpawn(
            task_id="t1",
            task_title="already running",
            process=mock_proc,
            lease_id="lease_t1",
            started_at=time.time(),
            lane_name="hermes-t1",
        )

        # Main returns t1 again (re-poll before completion) plus a new task t2
        mock_tasks = [
            {"id": "t1", "title": "already running", "status": "running",
             "assigned_to": "my-node", "priority": 1},
            {"id": "t2", "title": "new task", "status": "running",
             "assigned_to": "my-node", "priority": 2},
        ]

        with patch("hermes_cluster.core.agent_executor._signed_request",
                    return_value=mock_tasks):
            with patch.object(executor, "_spawn_worker") as mock_spawn:
                executor._claim_and_spawn(max_spawns=3)
                # t1 must NOT be re-spawned; only t2 should spawn
                spawned_ids = [call.args[0]["id"] for call in mock_spawn.call_args_list]
                assert "t1" not in spawned_ids, (
                    f"Dedup guard failed: t1 was re-spawned (spawned={spawned_ids})"
                )
                assert "t2" in spawned_ids, (
                    f"New task t2 was not spawned (spawned={spawned_ids})"
                )
                assert mock_spawn.call_count == 1

    def test_claim_and_spawn_filters_correctly(self):
        """_claim_and_spawn only spawns for tasks assigned to this node."""
        cfg = AgentExecutorConfig(enabled=True, max_concurrent=2)
        executor = AgentExecutor(
            config=cfg,
            node_id="my-node",
            cluster_endpoint="http://127.0.0.1:9999",
        )

        mock_tasks = [
            {"id": "t1", "title": "for me", "status": "running", "assigned_to": "my-node", "priority": 1},
            {"id": "t2", "title": "for other", "status": "running", "assigned_to": "other-node", "priority": 2},
            {"id": "t3", "title": "pending", "status": "pending", "assigned_to": None, "priority": 3},
            {"id": "t4", "title": "also for me", "status": "running", "assigned_to": "my-node", "priority": 2},
        ]

        with patch("hermes_cluster.core.agent_executor._signed_request", return_value=mock_tasks):
            with patch.object(executor, "_spawn_worker") as mock_spawn:
                executor._claim_and_spawn(max_spawns=2)
                # Should only spawn for t1 and t4 (assigned to my-node, status=running)
                assert mock_spawn.call_count == 2
                spawned_ids = [call.args[0]["id"] for call in mock_spawn.call_args_list]
                assert "t1" in spawned_ids
                assert "t4" in spawned_ids
                assert "t2" not in spawned_ids
                assert "t3" not in spawned_ids
