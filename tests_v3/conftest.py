"""Shared fixtures for the store parity suite (#829).

Postgres availability guard: tests that need the Postgres backend call the
``pg_dsn`` fixture; if the env var is unset or no server answers, they SKIP.
CI (python-tests job) runs postgres:16 as a service and sets
HERMES_CLUSTER_PG_TEST_DSN, so the guard is satisfied there. Locally, point
it at a throwaway container (password lives in the shell env only — never in
the repo):

    docker run -d --name hermes-test-pg -e POSTGRES_PASSWORD=... \
      -e POSTGRES_DB=hermes_test -p 55432:5432 postgres:16-alpine
    export HERMES_CLUSTER_PG_TEST_DSN=postgresql://USER:***@localhost:55432/hermes_test
"""

from __future__ import annotations

import os

import pytest


def _pg_available(dsn: str) -> bool:
    try:
        import asyncio

        import asyncpg

        async def _ping():
            conn = await asyncpg.connect(dsn, timeout=3)
            await conn.execute("SELECT 1")
            await conn.close()

        asyncio.run(_ping())
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    """DSN for the throwaway test database, or skip the whole test.

    CI-hardening (round-2 review): if HERMES_CLUSTER_PG_TEST_DSN IS set,
    the environment claims a server exists — an unreachable one is then a
    CI misconfiguration (dead service container, wrong port), not a
    "postgres unavailable locally" case, so FAIL loudly instead of
    skipping. A silently-skipping pg suite is worse than no suite: it
    would mark the Postgres leg green while never running it. Without the
    env var (local sqlite-only dev), skipping stays the correct behaviour.
    """
    dsn = os.environ.get("HERMES_CLUSTER_PG_TEST_DSN", "")
    if not dsn:
        pytest.skip(
            "Postgres unavailable (set HERMES_CLUSTER_PG_TEST_DSN; "
            "CI provides a postgres:16 service — see module docstring)"
        )
    if not _pg_available(dsn):
        pytest.fail(
            "HERMES_CLUSTER_PG_TEST_DSN is set but no server answers — "
            "the Postgres test leg would silently no-op. Check the CI "
            "service container (ci.yml: postgres:16, health-checked) "
            "or unset the variable for a sqlite-only local run."
        )
    return dsn


@pytest.fixture()
def pg_store(pg_dsn):
    """Fresh SyncPostgresStore with the schema truncated between tests."""
    from hermes_cluster.state.postgres_store import SyncPostgresStore

    store = SyncPostgresStore(dsn=pg_dsn)
    store.truncate_all()  # bridged coroutine (sync facade)
    yield store
    store.close()
