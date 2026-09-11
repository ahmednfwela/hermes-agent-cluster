"""Store parity suite (#829): the SAME behavioural tests run against both
backends — SQLite (ClusterStore, the local/dev default) and Postgres
(PostgresClusterStore behind the sync facade, selected by
``store.backend: postgres``).

Postgres tests skip cleanly when no server is reachable (conftest.pg_dsn);
CI (python-tests job) provides postgres:16 as a service so the guard passes.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from hermes_cluster.models import (
    BatchSyncMessage,
    Delivery,
    DeliveryStatus,
    EventType,
    LeaseStatus,
    Node,
    NodeStatus,
    RecoveryEvent,
    SchedulingDecision,
    SyncMessage,
    TaskStatus,
)
from hermes_cluster.state import ClusterState
from hermes_cluster.state.cluster_store import ClusterStore

# ---------------------------------------------------------------------------
# Backend fixtures
# ---------------------------------------------------------------------------


def make_sqlite_store():
    return ClusterStore(":memory:")


BACKENDS = ["sqlite"]


@pytest.fixture()
def store(request):
    """Parameterised over available backends; postgres skipped if no server.

    pg_store is resolved LAZILY (request.getfixturevalue) so sqlite-only runs
    never trigger the Postgres availability check.
    """
    if request.param == "sqlite":
        s = make_sqlite_store()
        yield s
        s.close()
    else:
        yield request.getfixturevalue("pg_store")


def _backend_params():
    return [
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres"),
    ]


# ---------------------------------------------------------------------------
# Core task lifecycle (identical assertions on both backends)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store", _backend_params(), indirect=True)
class TestTaskLifecycle:
    def test_create_task_promotes_to_ready(self, store):
        t = store.create_task("t1", "one", ["tooling"])
        assert t is not None
        fetched = store.get_task("t1")
        assert fetched.title == "one"
        assert fetched.requires == ["tooling"]
        assert fetched.status == TaskStatus.ready
        assert fetched.version == 1

    def test_create_task_idempotent(self, store):
        store.create_task("t1", "first", [])
        store.create_task("t1", "second", [])
        assert store.get_task("t1").title == "first"

    def test_set_task_status_and_version_bump(self, store):
        store.create_task("t1", "x", [])
        assert store.set_task_status("t1", TaskStatus.running) is True
        task = store.get_task("t1")
        assert task.status == TaskStatus.running
        assert task.version == 2
        assert store.set_task_status("missing", TaskStatus.running) is False

    def test_set_task_status_fail_reason(self, store):
        store.create_task("t1", "x", [])
        store.set_task_status("t1", TaskStatus.failed, fail_reason="boom")
        assert store.get_task("t1").fail_reason == "boom"

    def test_task_counts(self, store):
        for i in range(3):
            store.create_task(f"t{i}", f"t{i}", [])
        store.create_task("d", "dep", [])
        store.set_dependencies("d", ["t0"])
        counts = store.task_counts()
        assert counts["total"] == 4
        assert counts["ready"] == 3
        assert counts["pending"] == 1

    def test_dependencies_promotion(self, store):
        store.create_task("a", "a", [])
        store.create_task("b", "b", [])
        store.set_dependencies("b", ["a"])
        assert store.get_task("b").status == TaskStatus.pending
        assert store.trigger_pending_tasks() == 0  # dep not done
        store.set_task_status("a", TaskStatus.completed)
        assert store.trigger_pending_tasks() == 1
        assert store.get_task("b").status == TaskStatus.ready

    def test_get_dependents_and_chain(self, store):
        for t in ("a", "b", "c"):
            store.create_task(t, t, [])
        store.set_dependencies("b", ["a"])
        store.set_dependencies("c", ["b"])
        assert store.get_dependents("a") == ["b"]
        chain = store.get_trigger_chain("a")
        assert chain == ["b", "c"] or set(chain) == {"b", "c"}

    def test_workflow_graph(self, store):
        store.create_task("a", "a", [])
        store.create_task("b", "b", [])
        store.set_dependencies("b", ["a"])
        g = store.get_workflow_graph()
        assert {n["id"] for n in g["nodes"]} == {"a", "b"}
        assert {"from": "a", "to": "b"} in g["edges"]

    def test_lane_fields_round_trip(self, store):
        store.create_task("t1", "x", [], lane_key="repo#branch", role="reviewer")
        t = store.get_task("t1")
        assert t.lane_key == "repo#branch"
        assert t.role == "reviewer"


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store", _backend_params(), indirect=True)
class TestNodes:
    def _node(self, nid, caps=("tooling",), maxc=0):
        return Node(id=nid, name=nid, capabilities=list(caps),
                    status=NodeStatus.online, max_concurrent=maxc)

    def test_register_get_update(self, store):
        store.register_node(self._node("n1"))
        n = store.get_node("n1")
        assert n.capabilities == ["tooling"]
        assert store.node_count() == 1
        store.update_heartbeat("n1", load=0.5)
        n = store.get_node("n1")
        assert n.load == 0.5
        assert n.status == NodeStatus.online
        store.set_node_status("n1", NodeStatus.offline)
        assert store.get_node("n1").status == NodeStatus.offline
        assert store.online_count() == 0

    def test_register_is_upsert(self, store):
        store.register_node(self._node("n1", caps=["a"]))
        store.register_node(self._node("n1", caps=["b"], maxc=3))
        n = store.get_node("n1")
        assert n.capabilities == ["b"]
        assert n.max_concurrent == 3

    def test_capability_callback(self, store):
        seen = []
        store.register_node(self._node("n1", caps=["a"]))
        store.set_on_capability_change(lambda nid, old, new: seen.append((nid, old, new)))
        store.update_capabilities("n1", ["a", "b"])
        assert seen == [("n1", ["a"], ["a", "b"])]


# ---------------------------------------------------------------------------
# Scheduling (the fairness guarantees ported with the advisory lock)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store", _backend_params(), indirect=True)
class TestScheduling:
    def _register(self, store, nid):
        store.register_node(Node(id=nid, name=nid, capabilities=["tooling"],
                                 status=NodeStatus.online))

    def test_spread_least_loaded(self, store):
        for nid in ("n1", "n2", "n3"):
            self._register(store, nid)
        for i in range(6):
            store.create_task(f"t{i}", f"t{i}", ["tooling"])
        assert store.schedule_pending() == 6
        counts = {}
        for i in range(6):
            node = store.get_task(f"t{i}").assigned_to
            counts[node] = counts.get(node, 0) + 1
        assert sorted(counts.values()) == [2, 2, 2]

    def test_active_lease_blocks_reschedule(self, store):
        self._register(store, "n1")
        store.create_task("t1", "t1", ["tooling"])
        store.create_lease("t1", "n1", timedelta(minutes=5))
        assert store.schedule_pending() == 0

    def test_max_concurrent_ceiling(self, store):
        store.register_node(Node(id="n1", name="n1", capabilities=["tooling"],
                                 status=NodeStatus.online, max_concurrent=1))
        store.create_task("t1", "t1", ["tooling"])
        store.create_task("t2", "t2", ["tooling"])
        assert store.schedule_pending() == 1

    def test_schedule_detailed_reports_only_new(self, store):
        self._register(store, "n1")
        store.create_task("t1", "t1", ["tooling"])
        first = store.schedule_pending_detailed()
        assert len(first) == 1 and first[0]["task_id"] == "t1"
        assert store.schedule_pending_detailed() == []

    def test_decisions_recorded_and_trimmed(self, store):
        self._register(store, "n1")
        for i in range(5):
            store.create_task(f"t{i}", f"t{i}", ["tooling"])
        store.schedule_pending()
        stats = store.get_schedule_stats()
        assert stats.total_decisions == 5
        # Trim path: push past the cap one-by-one
        for i in range(250):
            store.record_decision(SchedulingDecision(
                task_id=f"x{i}", task_title="x", priority=3,
                node_id="n1", score=1.0, reason="test"))
        assert len(store.get_decisions()) <= 200


# ---------------------------------------------------------------------------
# Leases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store", _backend_params(), indirect=True)
class TestLeases:
    def test_create_lookup_revoke(self, store):
        store.create_task("t1", "t1", [])
        lease = store.create_lease("t1", "n1", timedelta(seconds=60))
        assert lease is not None
        assert store.get_lease_by_task("t1").id == lease.id
        assert len(store.get_active_leases()) == 1
        assert store.revoke_lease(lease.id) is True
        assert store.get_lease_by_task("t1") is None
        assert store.revoke_lease("nope") is False

    def test_expiry_marks_and_fires_callback(self, store):
        store.create_task("t1", "t1", [])
        fired = []
        store.set_lease_callback(lambda tid, nid: fired.append((tid, nid)))
        store.create_lease("t1", "n1", timedelta(seconds=-5))  # already expired
        assert store.get_active_leases() == []
        assert fired == [("t1", "n1")]
        expired = store.get_expired_leases()
        assert len(expired) == 1
        assert expired[0].status == LeaseStatus.expired

    def test_extend_overlap_tolerated(self, store):
        """LeaseManager.extend() creates the new lease BEFORE revoking the old
        one — both stores must tolerate two active leases on one task briefly."""
        store.create_task("t1", "t1", [])
        l1 = store.create_lease("t1", "n1", timedelta(seconds=60))
        l2 = store.create_lease("t1", "n1", timedelta(seconds=60))
        assert l1.id != l2.id
        store.revoke_lease(l1.id)
        actives = [l for l in store.get_active_leases() if l.task_id == "t1"]
        assert [l.id for l in actives] == [l2.id]


# ---------------------------------------------------------------------------
# Sync (version-gated LWW)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store", _backend_params(), indirect=True)
class TestSync:
    def test_version_gate(self, store):
        m = lambda v: SyncMessage(version=v, sender_node="n2")
        assert store.handle_sync_message(m(5)) is True
        assert store.handle_sync_message(m(3)) is False   # older
        assert store.handle_sync_message(m(5)) is False   # equal
        assert store.handle_sync_message(m(6)) is True
        assert store.sync_version() == 6

    def test_task_state_apply(self, store):
        from hermes_cluster.models import TaskSync
        msg = SyncMessage(
            version=10, sender_node="n2",
            task_state=TaskSync(task_id="ts1", title="synced",
                                status="running", assigned_to="n2", version=4),
        )
        assert store.handle_sync_message(msg) is True
        t = store.get_task("ts1")
        assert t.status == TaskStatus.running
        assert t.assigned_to == "n2"
        # replay with new version updates it
        msg2 = msg.model_copy(update={"version": 11})
        store.handle_sync_message(msg2)
        assert store.get_task("ts1").version == 4

    def test_batch_sync(self, store):
        batch = BatchSyncMessage(messages=[SyncMessage(version=i) for i in range(1, 4)])
        assert store.handle_batch_sync(batch) == 3


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store", _backend_params(), indirect=True)
class TestRecovery:
    def test_append_and_stats(self, store):
        store.append_recovery_event(RecoveryEvent(
            id="r1", task_id="t1", node_id="n1",
            action="revoke_lease", status="completed"))
        store.trigger_recovery("n2")
        events = store.get_recovery_events()
        assert len(events) == 2
        assert events[0].action == "revoke_lease"
        stats = store.recovery_stats()
        assert stats["total"] == 2
        assert stats["by_action"]["reschedule"] == 1


# ---------------------------------------------------------------------------
# Federation / hooks / deliveries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store", _backend_params(), indirect=True)
class TestFederationAndHooks:
    def test_federation_crud(self, store):
        c = store.register_federation_cluster("fc1", "peer", "https://x")
        assert c.status.value == "available"
        assert store.get_federation_cluster("fc1").endpoint == "https://x"
        store.register_federation_cluster("fc1", "peer2", "https://y")
        assert store.get_federation_cluster("fc1").name == "peer2"
        assert len(store.get_federation_clusters()) == 1
        assert store.remove_federation_cluster("fc1") is True
        assert store.remove_federation_cluster("fc1") is False

    def test_hooks_secrets_hidden(self, store):
        h = store.register_hook("https://hook", [EventType.task_completed],
                                secret="s3cr3t")
        listed = store.list_hooks()
        assert len(listed) == 1
        assert listed[0].secret is None  # never exposed via list
        assert store.deregister_hook(h.id) is True
        assert store.list_hooks() == []

    def test_deliveries_trim_and_lookup(self, store):
        h = store.register_hook("https://hook", [EventType.task_completed])
        for i in range(3):
            store.add_delivery(Delivery(
                id=f"d{i}", hook_id=h.id, event_type="task_completed",
                payload={"n": i}, status=DeliveryStatus.delivered))
        dels = store.get_hook_deliveries(h.id)
        assert len(dels) == 3
        assert {d.payload["n"] for d in dels} == {0, 1, 2}
        # trim past the cap
        for i in range(1005):
            store.add_delivery(Delivery(
                id=f"big{i}", hook_id=h.id, event_type="x",
                payload={}, status=DeliveryStatus.delivered))
        assert len(store.get_hook_deliveries(h.id)) <= 1000


# ---------------------------------------------------------------------------
# Spawns + lanes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store", _backend_params(), indirect=True)
class TestSpawnsAndLanes:
    def test_spawn_round_trip(self, store):
        store.record_task_spawn("t1", job_id="j1", pid=4242, started_at=123.0,
                                lane_key="lane", role="reviewer", session_id="s1")
        rec = store.get_task_spawn("t1")
        assert rec["pid"] == 4242
        assert rec["lane_key"] == "lane"
        assert rec["session_id"] == "s1"
        store.record_task_spawn("t1", job_id="j2")  # upsert
        assert store.get_task_spawn("t1")["job_id"] == "j2"
        assert len(store.get_all_task_spawns()) == 1
        assert store.delete_task_spawn("t1") is True
        assert store.get_task_spawn("t1") is None

    def test_lane_created_at_wins_first(self, store):
        store.record_lane("L1", session_id="s1", created_at=100.0)
        first = store.get_lane("L1")
        assert first["created_at"] == pytest.approx(100.0)
        store.record_lane("L1", session_id="s2", created_at=999.0)
        second = store.get_lane("L1")
        assert second["created_at"] == pytest.approx(100.0)  # first wins
        assert second["session_id"] == "s2"                  # fields update
        assert second["last_active_at"] >= first["last_active_at"]

    def test_lane_touch_delete(self, store):
        store.record_lane("L1")
        store.touch_lane_last_task("L1", "task9")
        assert store.get_lane("L1")["last_task_id"] == "task9"
        store.touch_lane_last_task("missing", "task9")  # no-op, must not raise
        assert store.delete_lane("L1") is True
        assert store.get_lane("L1") is None


# ---------------------------------------------------------------------------
# Config + summary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store", _backend_params(), indirect=True)
class TestConfigSummary:
    def test_config_round_trip(self, store):
        store.set_config({"agent_executor": {"enabled": True}})
        cfg = store.get_config()
        assert cfg["agent_executor"]["enabled"] is True

    def test_summary_shape(self, store):
        store.cluster_id = "c1"
        store.node_id = "m1"
        store.register_node(Node(id="n1", name="n1", capabilities=[],
                                 status=NodeStatus.online))
        store.create_task("t1", "t1", [])
        s = store.get_summary()
        assert s["cluster_id"] == "c1"
        assert s["nodes"] == {"total": 1, "online": 1}
        assert s["tasks"]["total"] == 1
        assert "sync_version" in s and "uptime_seconds" in s


# ---------------------------------------------------------------------------
# The unassign_task guards (terminal + live-lease) — the #833 invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store", _backend_params(), indirect=True)
class TestUnassignGuards:
    def test_unassign_respects_live_lease(self, store):
        store.create_task("t1", "t1", [])
        store.set_task_status("t1", TaskStatus.running)
        store.create_lease("t1", "n1", timedelta(minutes=5))
        assert store.unassign_task("t1") is False
        assert store.get_task("t1").status == TaskStatus.running

    def test_unassign_after_lease_expiry(self, store):
        store.create_task("t1", "t1", [])
        store.set_task_status("t1", TaskStatus.running)
        lease = store.create_lease("t1", "n1", timedelta(seconds=-1))
        store.get_active_leases()  # side-effect: marks expired (both backends)
        assert store.unassign_task("t1") is True
        assert store.get_task("t1").status == TaskStatus.ready
        assert store.get_task("t1").assigned_to is None

    def test_terminal_never_revived(self, store):
        store.create_task("t1", "t1", [])
        store.set_task_status("t1", TaskStatus.completed)
        assert store.unassign_task("t1") is False
        assert store.get_task("t1").status == TaskStatus.completed

    def test_unblock(self, store):
        store.create_task("t1", "t1", [])
        store.set_task_status("t1", TaskStatus.blocked)
        assert store.unblock_task("t1") is True
        assert store.get_task("t1").status == TaskStatus.pending
        assert store.unblock_task("t1") is False  # no longer blocked
