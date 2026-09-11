"""Importer tests (#829 req. 3): cluster.db -> Postgres loses nothing.

The fixture SQLite DB exercises the real legacy shapes: ISO-TEXT datetimes,
JSON TEXT columns, AUTOINCREMENT sync ids, the lane/spawn tables. The
Postgres halves skip without a server (CI provides one).
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from hermes_cluster.models import (
    BatchSyncMessage,
    Delivery,
    DeliveryStatus,
    EventType,
    Node,
    NodeStatus,
    SchedulingDecision,
    SyncMessage,
    TaskStatus,
)
from hermes_cluster.state.cluster_store import ClusterStore
from hermes_cluster.tools.import_sqlite import _parse_dt, _upsert_sql, import_sqlite


TABLES = (
    "nodes", "tasks", "lanes", "leases", "sync_log", "recovery_events",
    "scheduling_decisions", "federation_clusters", "hooks", "deliveries",
    "kv_store", "task_spawns",
)


def _build_source(tmp_path) -> str:
    """Populate a cluster.db across EVERY table with non-trivial rows."""
    db = str(tmp_path / "cluster.db")
    store = ClusterStore(db_path=db)

    store.register_node(Node(id="n1", name="node one", capabilities=["tooling", "planning"],
                             status=NodeStatus.online, load=0.25, max_concurrent=2))
    store.register_node(Node(id="n2", name="node two", capabilities=["reviewing"],
                             status=NodeStatus.degraded))

    store.create_task("t1", "first task", ["tooling"], priority=1,
                      lane_key="repo#br", role="reviewer")
    store.create_task("t2", "second task", [])
    store.set_dependencies("t2", ["t1"])
    store.set_task_status("t1", TaskStatus.completed)
    store.trigger_pending_tasks()  # t2 -> ready

    lease = store.create_lease("t1", "n1", timedelta(minutes=5))
    store.revoke_lease(lease.id)

    store.handle_batch_sync(BatchSyncMessage(messages=[
        SyncMessage(version=1, sender_node="n2"),
        SyncMessage(version=2, sender_node="n2"),
    ]))

    store.trigger_recovery("n9")
    store.record_decision(SchedulingDecision(
        task_id="t1", task_title="first task", priority=1,
        node_id="n1", score=0.9, reason="test"))

    store.register_federation_cluster("fc1", "peer cluster", "https://peer")
    hook = store.register_hook("https://hook", [EventType.task_completed], secret="***")
    store.add_delivery(Delivery(id="d1", hook_id=hook.id, event_type="task_completed",
                                payload={"k": "v"}, status=DeliveryStatus.delivered))
    store.record_task_spawn("t1", job_id="j9", pid=1234, started_at=999.5,
                            lane_key="repo#br", role="reviewer", session_id="sess1")
    store.record_lane("repo#br", session_id="sess1", profile="bdaya-worker",
                      role="author", node="n1", created_at=123.0,
                      last_active_at=456.0, last_task_id="t1")
    store.set_config({"store": {"backend": "sqlite"}, "x": [1, 2, 3]})
    store.close()
    return db


def _source_counts(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in TABLES}
    conn.close()
    return counts


def _truncate(pg_dsn):
    """Drop+recreate the schema so each test starts clean (shares CI DB with
    the parity suite)."""
    from hermes_cluster.state.postgres_store import PostgresClusterStore

    async def _do():
        s = PostgresClusterStore(dsn=pg_dsn)
        await s.connect()
        try:
            await s.truncate_all()
        finally:
            await s.close()

    asyncio.run(_do())


# ---------------------------------------------------------------------------
# Pure-logic tests (no Postgres needed)
# ---------------------------------------------------------------------------


def test_upsert_sql_shape():
    sql = _upsert_sql("kv_store", ["key", "value"])
    assert '"key"' in sql and "ON CONFLICT" in sql
    sql = _upsert_sql("tasks", ["id", "title"])
    assert sql.startswith("INSERT INTO tasks (id, title)")


def test_dt_parsing():
    naive = datetime(2026, 9, 11, 12, 0, 0)
    out = _parse_dt(naive.isoformat())
    assert out.tzinfo is timezone.utc and out.hour == 12
    assert _parse_dt("") is None
    assert _parse_dt(None) is None


# ---------------------------------------------------------------------------
# End-to-end import against Postgres (skip without a server)
# ---------------------------------------------------------------------------


def test_import_loses_nothing(tmp_path, pg_dsn):
    db = _build_source(tmp_path)
    src_counts = _source_counts(db)

    # Clean slate: this test shares the CI database with the parity suite.
    _truncate(pg_dsn)
    counts = asyncio.run(import_sqlite(db, pg_dsn))
    assert counts == src_counts, "importer dropped or duplicated rows"

    from hermes_cluster.state.postgres_store import SyncPostgresStore

    store = SyncPostgresStore(dsn=pg_dsn)
    try:
        nodes = {n.id: n for n in store.get_all_nodes()}
        assert set(nodes) == {"n1", "n2"}
        assert nodes["n1"].capabilities == ["tooling", "planning"]
        assert nodes["n1"].max_concurrent == 2
        assert nodes["n2"].status == NodeStatus.degraded
        assert nodes["n1"].load == pytest.approx(0.25)

        tasks = {t.id: t for t in store.get_all_tasks()}
        assert set(tasks) >= {"t1", "t2"}
        assert tasks["t1"].lane_key == "repo#br"
        assert tasks["t1"].role == "reviewer"
        assert tasks["t1"].status == TaskStatus.completed
        assert tasks["t2"].depends_on == ["t1"]
        assert tasks["t2"].status == TaskStatus.ready

        lane = store.get_lane("repo#br")
        assert lane["created_at"] == pytest.approx(123.0)
        assert lane["session_id"] == "sess1"

        assert store.sync_version() == 2
        spawn = store.get_task_spawn("t1")
        assert spawn["pid"] == 1234 and spawn["session_id"] == "sess1"

        assert store.get_config() == {"store": {"backend": "sqlite"}, "x": [1, 2, 3]}

        hooks = store.list_hooks()
        assert len(hooks) == 1 and hooks[0].secret is None
        assert store.get_hook_deliveries(hooks[0].id)[0].payload == {"k": "v"}

        assert store.get_federation_cluster("fc1").name == "peer cluster"
        stats = store.recovery_stats()
        assert stats["total"] == 1 and stats["by_action"]["reschedule"] == 1
        decisions = store.get_decisions()
        assert len(decisions) == 1 and decisions[0].reason == "test"

        # The revoked lease survives as revoked.
        assert store.get_lease_by_task("t1") is None
        assert store.get_active_leases() == []
    finally:
        store.close()


def test_import_is_idempotent(tmp_path, pg_dsn):
    db = _build_source(tmp_path)
    src_counts = _source_counts(db)

    _truncate(pg_dsn)
    asyncio.run(import_sqlite(db, pg_dsn))
    counts2 = asyncio.run(import_sqlite(db, pg_dsn))  # re-run must not dupe
    assert counts2 == src_counts

    from hermes_cluster.state.postgres_store import SyncPostgresStore

    store = SyncPostgresStore(dsn=pg_dsn)
    try:
        assert len(store.get_all_tasks()) == src_counts["tasks"]
        assert store.sync_version() == 2
    finally:
        store.close()
