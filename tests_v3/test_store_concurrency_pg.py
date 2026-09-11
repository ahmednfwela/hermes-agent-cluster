"""Cross-node concurrency guarantees (#829) — Postgres only.

Two (or more) simultaneous clients of the SAME database = the multi-node
case SQLite's single process lock never had to handle. Each store instance
has its own connection pool and its own loop thread, so contention here is
real client contention. Skips without a reachable server (conftest.pg_dsn);
CI's python-tests job provides postgres:16.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from hermes_cluster.models import (
    Node,
    NodeStatus,
    SyncMessage,
    TaskStatus,
)


@pytest.fixture()
def two_stores(pg_dsn):
    """Two independent PostgresClusterStore 'nodes' on a clean schema."""
    from hermes_cluster.state.postgres_store import PostgresClusterStore, SyncPostgresStore

    a = SyncPostgresStore(dsn=pg_dsn)
    # fresh schema, then a second client pointed at it
    a.truncate_all()
    b = SyncPostgresStore(dsn=pg_dsn)
    yield a, b
    a.close()
    b.close()


def _register(store, nid):
    store.register_node(Node(id=nid, name=nid, capabilities=["tooling"],
                             status=NodeStatus.online))


class TestConcurrentNodes:
    def test_parallel_schedule_never_double_assigns(self, two_stores):
        """Two mains calling schedule_pending concurrently must not assign one
        ready task twice — the SQLite store got this from its process lock;
        Postgres gets it from pg_advisory_xact_lock. If the advisory lock were
        missing, stale load snapshots interleave and a task can go running on
        two nodes."""
        a, b = two_stores
        _register(a, "n1")
        _register(b, "n2")
        for i in range(20):
            a.create_task(f"t{i}", f"t{i}", ["tooling"])

        results = {}
        with ThreadPoolExecutor(max_workers=2) as ex:
            fa = ex.submit(a.schedule_pending_detailed)
            fb = ex.submit(b.schedule_pending_detailed)
            results["a"] = fa.result()
            results["b"] = fb.result()

        assigned = {}
        for r in results["a"] + results["b"]:
            assert r["task_id"] not in assigned, (
                f"task {r['task_id']} double-assigned across nodes "
                f"-> {assigned.get(r['task_id'])} and {r['node_id']}")
            assigned[r["task_id"]] = r["node_id"]
        assert len(assigned) == 20, "every task scheduled exactly once"
        # cross-check the stored state agrees
        for tid in assigned:
            t = a.get_task(tid)
            assert t.status == TaskStatus.running
            assert t.assigned_to == assigned[tid]

    def test_concurrent_same_version_sync_single_winner(self, two_stores):
        """Exactly one node may apply a given sync version (LWW gate held
        across nodes, not just in-process)."""
        a, b = two_stores
        new_msg = lambda: SyncMessage(version=42, sender_node="peer")
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(fn, new_msg())
                    for fn in [a.handle_sync_message] * 4 + [b.handle_sync_message] * 4]
            outcomes = [f.result() for f in futs]
        assert sum(1 for o in outcomes if o) == 1, (
            f"{outcomes.count(True)} winners for one version; expected 1")
        assert a.sync_version() == 42

    def test_concurrent_create_task_idempotent(self, two_stores):
        """The same task id hammered from both nodes yields one row."""
        a, b = two_stores
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = []
            for i, store in enumerate([a, b] * 6):
                futs.append(ex.submit(store.create_task, "dup", f"title{store is a}", ["x"]))
            [f.result() for f in futs]
        tasks = a.get_all_tasks()
        assert len([t for t in tasks if t.id == "dup"]) == 1

    def test_lease_expiry_race_marks_once(self, two_stores):
        """Two nodes scanning the same expired lease both see it expired, and
        the marking UPDATE is safe to run concurrently."""
        a, b = two_stores
        a.create_task("t1", "t1", [])
        a.create_lease("t1", "n1", timedelta(seconds=-1))
        fired = []
        a.set_lease_callback(lambda tid, nid: fired.append(("a", tid)))
        b.set_lease_callback(lambda tid, nid: fired.append(("b", tid)))
        ra, rb = [], []
        t1 = threading.Thread(target=lambda: ra.append(a.get_active_leases()))
        t2 = threading.Thread(target=lambda: rb.append(b.get_active_leases()))
        t1.start(); t2.start(); t1.join(timeout=30); t2.join(timeout=30)
        assert ra == [[]] and rb == [[]]
        assert len(fired) == 2  # both scanners observed expiry (callbacks are per-node)
        assert len(b.get_expired_leases()) == 1  # marked exactly once each

    def test_unassign_guard_holds_under_live_lease(self, two_stores):
        """The #833 live-lease guard on unassign_task is a single SQL
        predicate — evaluated the same regardless of which node runs it."""
        a, b = two_stores
        a.create_task("t1", "t1", [])
        a.set_task_status("t1", TaskStatus.running)
        a.create_lease("t1", "n1", timedelta(minutes=5))
        assert b.unassign_task("t1") is False      # lease live: refuse
        assert a.unassign_task("t1") is False
        assert a.get_task("t1").status == TaskStatus.running
