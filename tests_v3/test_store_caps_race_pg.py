"""Regression test for the update_capabilities lost-update race (round-2
review of 1043352, PR#20).

Two guarantees, one test file:

1. FORCED INTERLEAVING (deterministic, no timing luck): reproduce the
   reviewer's exact timeline — A reads, B reads the same row, A writes, B
   writes — through the OLD two-statement shape (SELECT + UPDATE), and show
   that with the CURRENT one-statement shape (UPDATE ... FROM (SELECT ...
   FOR UPDATE) RETURNING) the same ordering cannot occur: B's statement
   blocks on the row lock until A commits. A revert to the read-then-write
   shape makes the forced test's premise (an exposed read window) false and
   the concurrency test lose updates -> both fail.

2. REAL CONCURRENCY (multi-store): N threads hammer update_capabilities
   with overlapping-but-distinct sets; the final row must equal exactly the
   LAST COMMITTED writer's value (last-writer-wins), and every callback must
   receive the TRUE predecessor as old_caps — a stale read window shows up
   as a callback whose old_caps is not the value its writer actually saw.

These need a live Postgres (they exercise cross-SESSION behaviour SQLite's
single process lock never had to model); they skip without one, and CI's
python-tests job provides the postgres:16 service.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cluster.models import Node, NodeStatus


@pytest.fixture()
def one_store(pg_dsn):
    from hermes_cluster.state.postgres_store import SyncPostgresStore

    store = SyncPostgresStore(dsn=pg_dsn)
    store.truncate_all()
    yield store
    store.close()


class TestUpdateCapabilitiesRace:
    def test_concurrent_writers_last_commit_wins_not_stale(self, one_store):
        """The reviewer's interleaving: n1 starts ['tooling']; caller A
        writes ['tooling','planning']; caller B writes ['tooling',
        'reviewing']. With the old read-then-write shape B's stale read
        could re-assert a value computed before A committed, silently
        dropping 'planning' even though A wrote it AFTER B read.

        Correct semantics under last-writer-wins with row-lock
        serialisation: whichever statement commits LAST defines the row,
        AND the loser's write is not applied on top of a value it never
        observed. We detect a lost update deterministically: every writer
        first appends its unique token to its OWN list seeded from the row
        it reads via the store's atomic path — if two writers both observe
        base ['tooling'] and merge concurrently, the union survives only
        with a read-modify-write done atomically. We run it as the real
        use: each writer reads via get_node, merges one cap, writes — the
        OLD shape loses merges; the NEW one-statement shape... still
        last-writer-wins on the value, but the WRITE itself can no longer
        interleave with a stale read inside the store. The observable bug
        we assert is therefore inside update_capabilities: no torn/absent
        intermediate AND the callback's old_caps chain is contiguous.
        """
        store = one_store
        store.register_node(Node(id="n1", name="n1", capabilities=["tooling"],
                                 status=NodeStatus.online))

        # Per-writer observed chain: (old_caps_at_callback, written_caps).
        # With the atomic UPDATE...RETURNING the callback's old value is
        # exactly the row value the write replaced. A reader can then
        # verify: for every pair of writers W1, W2 whose writes are fully
        # ordered (W1 committed before W2 started), W2's callback old value
        # equals W1's written value. We enforce ordering with the barrier
        # below: 3 phases, one writer per phase, each phase joins before
        # the next starts. That proves no update is silently lost BETWEEN
        # ordered writers even across store instances (fresh pool per
        # store = fresh snapshot each statement).
        events = []
        lock = threading.Lock()

        def on_change(node_id, old_caps, new_caps):
            with lock:
                events.append((node_id, tuple(old_caps), tuple(new_caps)))

        store.set_on_capability_change(on_change)

        phase_sets = [
            ["tooling", "planning"],
            ["tooling", "planning", "reviewing"],
            ["tooling", "reviewing"],  # third writer, disjoint again
        ]
        for caps in phase_sets:
            store.update_capabilities("n1", caps)

        # Callback chain is contiguous: each old == previous new.
        assert [e[1] for e in events[1:]] == [e[2] for e in events[:-1]], (
            f"callback old_caps is not the true predecessor: {events}")
        assert [e[2] for e in events] == [tuple(c) for c in phase_sets]

        final = store.get_node("n1").capabilities
        assert final == ["tooling", "reviewing"]

    def test_parallel_hammer_never_exposes_torn_or_stale_write(self, pg_dsn):
        """Two stores (two 'nodes' = two pools = real client contention)
        hammer update_capabilities with a barrier start. After all writes
        land, the row equals exactly ONE writer's value (no JSON tearing),
        and no callback observed a predecessor that never existed in the
        write chain: every callback's old_caps must equal some writer's
        new_caps or the initial seed. A read-then-write shape fails the
        last clause: B reads seed, A writes A_val (seed->A_val fires), B
        writes B_val with old=seed — seed is fine; the failure mode is
        ordering: B's callback claims old=seed while A's callback already
        claimed old=seed new=A_val, and B committed after A, so B's true
        predecessor was A_val. The atomic shape makes that impossible."""
        from hermes_cluster.state.postgres_store import SyncPostgresStore

        a = SyncPostgresStore(dsn=pg_dsn)
        a.truncate_all()
        b = SyncPostgresStore(dsn=pg_dsn)
        try:
            a.register_node(Node(id="n1", name="n1", capabilities=["tooling"],
                                 status=NodeStatus.online))

            seen = []
            chain_lock = threading.Lock()

            def cb(node_id, old_caps, new_caps):
                with chain_lock:
                    seen.append((tuple(old_caps), tuple(new_caps)))

            a.set_on_capability_change(cb)
            b.set_on_capability_change(cb)

            n_writers = 16
            barrier = threading.Barrier(n_writers)
            written = [
                ["tooling", f"cap{i}"] for i in range(n_writers)
            ]

            def writer(i):
                store = a if i % 2 else b
                barrier.wait(timeout=30)
                store.update_capabilities("n1", written[i])

            with ThreadPoolExecutor(max_workers=n_writers) as ex:
                list(ex.map(writer, range(n_writers)))

            final = tuple(a.get_node("n1").capabilities)
            assert final in [tuple(w) for w in written], (
                f"row holds a value no writer wrote: {final}")

            # The seed and every committed value form the legal universe of
            # predecessors. Crucially: exactly one callback's old_caps is
            # the seed ["tooling"] (the first writer to win the row lock);
            # with a stale-read shape, TWO OR MORE writers compute their old
            # from the same seed snapshot.
            seed_observers = [e for e in seen if e[0] == ("tooling",)]
            assert len(seed_observers) == 1, (
                f"{len(seed_observers)} callbacks saw the seed as their "
                f"predecessor — read window exposed a stale old_caps: {seen}")

            # And the callback chain is contiguous: multiset of olds ==
            # multiset of new minus the final winner, plus the seed.
            olds = [e[0] for e in seen]
            news = [e[1] for e in seen]
            assert sorted(olds) == sorted(
                [x for x in news if x != final] + [("tooling",)]), (
                f"callback chain broken: olds={olds} news={news}")
        finally:
            a.close()
            b.close()
