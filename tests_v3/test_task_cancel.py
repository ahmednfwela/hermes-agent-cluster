"""Tests for two-phase task cancellation.

Covers:
- Cancel unclaimed (pending/ready) → cancelled immediately
- Cancel running → cancel_requested + lease revoked
- Worker ack (/complete) on cancel_requested → cancelled
- Cancel terminal → 409
- Scheduler skips cancel_requested
- Sync round-trips the new states
"""

import pytest
from fastapi.testclient import TestClient

from hermes_cluster.app import create_app
from hermes_cluster.models import TaskStatus, SyncEventType, EventType, SyncMessage, TaskSync
from hermes_cluster.state import ClusterState


@pytest.fixture
def app():
    """Create a test app with fresh state."""
    return create_app(
        cluster_id="test-cluster",
        node_id="test-node",
        node_role="main",
    )


@pytest.fixture
def client(app):
    """Create a test client."""
    return TestClient(app)


def _create_task(client, title="test-task"):
    """Helper: create a task and return its ID."""
    resp = client.post("/api/v1/tasks", json={"title": title})
    assert resp.status_code == 200
    return resp.json()["id"]


def _join_node(client, name="worker-1"):
    """Helper: join a node and return its ID."""
    resp = client.post("/api/v1/nodes/join", json={
        "node_name": name,
        "capabilities": ["coding"],
    })
    assert resp.status_code == 200
    return resp.json()["node_id"]


# ---------------------------------------------------------------------------
# 1. Cancel unclaimed → cancelled immediately
# ---------------------------------------------------------------------------

class TestCancelUnclaimed:
    def test_cancel_pending(self, client):
        """Cancelling a pending task transitions directly to cancelled."""
        task_id = _create_task(client)
        resp = client.post(f"/api/v1/tasks/{task_id}/cancel", json={"reason": "no longer needed"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cancelled"
        assert data["phase"] == "immediate"

        # Verify task state
        tasks = client.get("/api/v1/tasks").json()
        task = next(t for t in tasks if t["id"] == task_id)
        assert task["status"] == "cancelled"

    def test_cancel_ready(self, client):
        """Cancelling a ready task transitions directly to cancelled."""
        task_id = _create_task(client)
        # Task should be ready after creation (no deps → promoted)
        tasks = client.get("/api/v1/tasks").json()
        task = next(t for t in tasks if t["id"] == task_id)
        assert task["status"] in ("pending", "ready")

        resp = client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 2. Cancel running → cancel_requested + lease revoked
# ---------------------------------------------------------------------------

class TestCancelRunning:
    def test_cancel_running_revokes_lease(self, client):
        """Cancelling a running task revokes the lease and sets cancel_requested."""
        node_id = _join_node(client)
        task_id = _create_task(client)

        # Claim the task (pending → running)
        resp = client.post(f"/api/v1/tasks/{task_id}/claim", json={"node_id": node_id})
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"

        # Verify lease exists
        leases = client.get("/api/v1/leases").json()
        active_leases = [l for l in leases if l["task_id"] == task_id and l["status"] == "active"]
        assert len(active_leases) == 1

        # Cancel
        resp = client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cancel_requested"
        assert data["phase"] == "pending_ack"

        # Verify lease was revoked
        leases = client.get("/api/v1/leases").json()
        active_leases = [l for l in leases if l["task_id"] == task_id and l["status"] == "active"]
        assert len(active_leases) == 0

        # Verify task status
        tasks = client.get("/api/v1/tasks").json()
        task = next(t for t in tasks if t["id"] == task_id)
        assert task["status"] == "cancel_requested"


# ---------------------------------------------------------------------------
# 3. Worker ack → cancelled
# ---------------------------------------------------------------------------

class TestWorkerAck:
    def test_complete_on_cancel_requested(self, client):
        """Worker calling /complete on a cancel_requested task closes it to cancelled."""
        node_id = _join_node(client)
        task_id = _create_task(client)

        # Claim and cancel
        client.post(f"/api/v1/tasks/{task_id}/claim", json={"node_id": node_id})
        client.post(f"/api/v1/tasks/{task_id}/cancel")

        # Worker completes (ack)
        resp = client.post(f"/api/v1/tasks/{task_id}/complete")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

        # Verify final state
        tasks = client.get("/api/v1/tasks").json()
        task = next(t for t in tasks if t["id"] == task_id)
        assert task["status"] == "cancelled"

    def test_fail_on_cancel_requested(self, client):
        """Worker calling /fail on a cancel_requested task closes it to cancelled."""
        node_id = _join_node(client)
        task_id = _create_task(client)

        # Claim and cancel
        client.post(f"/api/v1/tasks/{task_id}/claim", json={"node_id": node_id})
        client.post(f"/api/v1/tasks/{task_id}/cancel")

        # Worker fails (ack)
        resp = client.post(f"/api/v1/tasks/{task_id}/fail", json={"reason": "couldn't finish"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 4. Cancel terminal → 409
# ---------------------------------------------------------------------------

class TestCancelTerminal:
    def test_cancel_completed_409(self, client):
        """Cancelling a completed task returns 409."""
        node_id = _join_node(client)
        task_id = _create_task(client)

        # Complete the task
        client.post(f"/api/v1/tasks/{task_id}/claim", json={"node_id": node_id})
        client.post(f"/api/v1/tasks/{task_id}/complete")

        resp = client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert resp.status_code == 409
        assert "not cancelable" in resp.json()["detail"]

    def test_cancel_failed_409(self, client):
        """Cancelling a failed task returns 409."""
        task_id = _create_task(client)
        client.post(f"/api/v1/tasks/{task_id}/fail", json={"reason": "broken"})

        resp = client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert resp.status_code == 409

    def test_cancel_already_cancelled_409(self, client):
        """Cancelling an already cancelled task returns 409."""
        task_id = _create_task(client)
        client.post(f"/api/v1/tasks/{task_id}/cancel")

        resp = client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert resp.status_code == 409

    def test_cancel_not_found_404(self, client):
        """Cancelling a non-existent task returns 404."""
        resp = client.post("/api/v1/tasks/nonexistent/cancel")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 5. Scheduler skips cancel_requested
# ---------------------------------------------------------------------------

class TestSchedulerSkip:
    def test_scheduler_skips_cancel_requested(self, client):
        """Scheduler must not assign a cancel_requested task."""
        node_id = _join_node(client)
        task_id = _create_task(client)

        # Cancel the task
        client.post(f"/api/v1/tasks/{task_id}/cancel")

        # Trigger scheduling
        resp = client.post("/api/v1/schedule/trigger")
        assert resp.status_code == 200

        # Verify task was NOT scheduled
        tasks = client.get("/api/v1/tasks").json()
        task = next(t for t in tasks if t["id"] == task_id)
        assert task["status"] == "cancelled"
        assert task["assigned_to"] is None

    def test_scheduler_does_not_reassign_cancel_requested(self, client):
        """Scheduler must not re-assign a cancel_requested task (was running)."""
        node_id = _join_node(client)
        task_id = _create_task(client)

        # Claim then cancel
        client.post(f"/api/v1/tasks/{task_id}/claim", json={"node_id": node_id})
        client.post(f"/api/v1/tasks/{task_id}/cancel")

        # Trigger scheduling
        client.post("/api/v1/schedule/trigger")

        # Verify task status unchanged
        tasks = client.get("/api/v1/tasks").json()
        task = next(t for t in tasks if t["id"] == task_id)
        assert task["status"] == "cancel_requested"


# ---------------------------------------------------------------------------
# 6. Sync round-trips new states
# ---------------------------------------------------------------------------

class TestSyncRoundTrip:
    def test_sync_cancel_requested(self):
        """Sync LWW carries cancel_requested state."""
        state = ClusterState()
        # Create task locally
        state.create_task("t1", "test", [], 3)

        # Receive sync with cancel_requested
        msg = SyncMessage(
            version=1,
            sender_node="remote-node",
            event_type=SyncEventType.task_cancel_requested,
            timestamp=1000,
            task_state=TaskSync(
                task_id="t1",
                title="test",
                status="cancel_requested",
                version=2,
            ),
        )
        applied = state.handle_sync_message(msg)
        assert applied is True

        task = state.get_task("t1")
        assert task.status == TaskStatus.cancel_requested

    def test_sync_cancelled(self):
        """Sync LWW carries cancelled state."""
        state = ClusterState()
        state.create_task("t1", "test", [], 3)

        msg = SyncMessage(
            version=1,
            sender_node="remote-node",
            event_type=SyncEventType.task_cancelled,
            timestamp=1000,
            task_state=TaskSync(
                task_id="t1",
                title="test",
                status="cancelled",
                version=2,
            ),
        )
        applied = state.handle_sync_message(msg)
        assert applied is True

        task = state.get_task("t1")
        assert task.status == TaskStatus.cancelled

    def test_sync_version_ordering(self):
        """Sync rejects older versions (LWW)."""
        state = ClusterState()
        state.create_task("t1", "test", [], 3)

        # Apply version 5
        msg1 = SyncMessage(
            version=5,
            sender_node="remote",
            event_type=SyncEventType.task_cancel_requested,
            timestamp=1000,
            task_state=TaskSync(task_id="t1", title="test", status="cancel_requested", version=5),
        )
        assert state.handle_sync_message(msg1) is True

        # Reject version 3 (older)
        msg2 = SyncMessage(
            version=3,
            sender_node="remote",
            event_type=SyncEventType.task_cancelled,
            timestamp=500,
            task_state=TaskSync(task_id="t1", title="test", status="cancelled", version=3),
        )
        assert state.handle_sync_message(msg2) is False

        # Still cancel_requested
        task = state.get_task("t1")
        assert task.status == TaskStatus.cancel_requested


# ---------------------------------------------------------------------------
# 7. Enum values
# ---------------------------------------------------------------------------

class TestEnums:
    def test_task_status_has_cancel_states(self):
        """TaskStatus enum includes cancel_requested and cancelled."""
        assert TaskStatus.cancel_requested.value == "cancel_requested"
        assert TaskStatus.cancelled.value == "cancelled"

    def test_sync_event_type_has_cancel_events(self):
        """SyncEventType includes cancel events."""
        assert SyncEventType.task_cancel_requested.value == "task_cancel_requested"
        assert SyncEventType.task_cancelled.value == "task_cancelled"

    def test_event_type_has_cancel_events(self):
        """EventType includes cancel events."""
        assert EventType.task_cancel_requested.value == "task_cancel_requested"
        assert EventType.task_cancelled.value == "task_cancelled"


# ---------------------------------------------------------------------------
# 7. Regression tests for review fixes (B1, B2, S1-S4)
# ---------------------------------------------------------------------------

class TestReviewFixes:
    """Tests added to close round-1 review findings."""

    def test_b1_advance_rejects_cancelled(self, client):
        """B1: /advance must reject a cancelled task (no revival)."""
        _join_node(client)
        task_id = _create_task(client)
        # Cancel immediately (no lease)
        client.post(f"/api/v1/tasks/{task_id}/cancel")
        # Try to advance
        resp = client.post(f"/api/v1/tasks/{task_id}/advance")
        assert resp.status_code == 409
        # Verify still cancelled
        tasks = client.get("/api/v1/tasks").json()
        task = next(t for t in tasks if t["id"] == task_id)
        assert task["status"] == "cancelled"

    def test_b1_advance_rejects_cancel_requested(self, client):
        """B1: /advance must reject a cancel_requested task."""
        node_id = _join_node(client)
        task_id = _create_task(client)
        # Claim then cancel (has lease → cancel_requested)
        client.post(f"/api/v1/tasks/{task_id}/claim", json={"node_id": node_id})
        client.post(f"/api/v1/tasks/{task_id}/cancel")
        # Try to advance
        resp = client.post(f"/api/v1/tasks/{task_id}/advance")
        assert resp.status_code == 409
        # Verify still cancel_requested
        tasks = client.get("/api/v1/tasks").json()
        task = next(t for t in tasks if t["id"] == task_id)
        assert task["status"] == "cancel_requested"

    def test_b2_complete_rejects_cancelled(self, client):
        """B2: /complete must reject an already-cancelled task (no overwrite)."""
        _join_node(client)
        task_id = _create_task(client)
        client.post(f"/api/v1/tasks/{task_id}/cancel")
        resp = client.post(f"/api/v1/tasks/{task_id}/complete")
        assert resp.status_code == 409
        # Verify still cancelled
        tasks = client.get("/api/v1/tasks").json()
        task = next(t for t in tasks if t["id"] == task_id)
        assert task["status"] == "cancelled"

    def test_b2_fail_rejects_cancelled(self, client):
        """B2: /fail must reject an already-cancelled task (no overwrite)."""
        _join_node(client)
        task_id = _create_task(client)
        client.post(f"/api/v1/tasks/{task_id}/cancel")
        resp = client.post(f"/api/v1/tasks/{task_id}/fail", json={"reason": "test"})
        assert resp.status_code == 409
        # Verify still cancelled
        tasks = client.get("/api/v1/tasks").json()
        task = next(t for t in tasks if t["id"] == task_id)
        assert task["status"] == "cancelled"

    def test_s2_cancel_branches_on_lease_not_status(self, client):
        """S2: cancel must branch on lease existence, not status."""
        node_id = _join_node(client)
        task_id = _create_task(client)
        # Claim (creates lease)
        client.post(f"/api/v1/tasks/{task_id}/claim", json={"node_id": node_id})
        # Cancel should go to cancel_requested (has lease)
        resp = client.post(f"/api/v1/tasks/{task_id}/cancel")
        data = resp.json()
        assert data["phase"] == "pending_ack"
        assert data["status"] == "cancel_requested"
        # Now cancel again (no lease after revoke) should 409 (already terminal)
        resp2 = client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert resp2.status_code == 409

    def test_s3_cluster_status_includes_cancel_states(self, client):
        """S3: /api/v1/cluster/status must surface cancel_requested/cancelled counts."""
        _join_node(client)
        task_id = _create_task(client)
        client.post(f"/api/v1/tasks/{task_id}/cancel")
        resp = client.get("/api/v1/cluster/status")
        data = resp.json()
        assert "cancelled" in data["tasks_by_status"]
        assert data["tasks_by_status"]["cancelled"] >= 1

    def test_s3_status_endpoint_includes_cancel_states(self, client):
        """S3: /api/v1/status must surface cancel_requested/cancelled in summary."""
        _join_node(client)
        task_id = _create_task(client)
        client.post(f"/api/v1/tasks/{task_id}/cancel")
        resp = client.get("/api/v1/status")
        data = resp.json()
        assert "cancelled" in data["summary"]
        assert data["summary"]["cancelled"] >= 1

    def test_s4_fail_cascades_cancel_to_dependents(self, client):
        """S4: /fail must cascade-cancel pending/ready dependents."""
        _join_node(client)
        parent_id = _create_task(client)
        child_id = _create_task(client)
        # Set dependency
        client.post(f"/api/v1/tasks/{child_id}/dependencies", json={"depends_on": [parent_id]})
        # Fail parent
        client.post(f"/api/v1/tasks/{parent_id}/fail", json={"reason": "test"})
        # Child should be cancelled
        tasks = client.get("/api/v1/tasks").json()
        child = next(t for t in tasks if t["id"] == child_id)
        assert child["status"] == "cancelled"

    def test_mutation_terminality_guard(self, client):
        """Mutation: removing terminality guard in set_task_status must break B1.

        This test documents the invariant: set_task_status must reject transitions
        FROM terminal states (except cancel_requested → cancelled). If the guard is
        removed, this test will fail because /advance will revive a cancelled task.
        """
        _join_node(client)
        task_id = _create_task(client)
        # Cancel immediately
        client.post(f"/api/v1/tasks/{task_id}/cancel")
        # Verify terminal
        tasks = client.get("/api/v1/tasks").json()
        task = next(t for t in tasks if t["id"] == task_id)
        assert task["status"] == "cancelled"
        # Try to advance — must be rejected (409)
        resp = client.post(f"/api/v1/tasks/{task_id}/advance")
        assert resp.status_code == 409, "terminality guard must reject advance on cancelled task"
