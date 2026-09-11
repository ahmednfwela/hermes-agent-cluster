"""ClusterStore backend factory — shared/claude-plugins#829.

Selects the store backend from configuration:

  store:
    backend: postgres          # or "sqlite" (default)
    dsn_env: HERMES_CLUSTER_PG_DSN   # env var holding the asyncpg DSN
    dsn: ...                   # NOT supported on purpose — a literal DSN
                               # (which embeds a password) must never live
                               # in the repo or a committed config file.

The DSN is taken (in order): ``dsn_env``-named env var, then ``DATABASE_URL``.
A ``postgresql://...`` DSN passed directly as a function argument is accepted
for tests only — production wiring must come from the environment (ESO
injects ``DATABASE_URL`` into the cluster-main pod; see #804 note 132531).

Never log the DSN: it contains the DB password.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def create_store(
    *,
    backend: str = "sqlite",
    db_path: str = "",
    dsn: str = "",
    dsn_env: str = "HERMES_CLUSTER_PG_DSN",
    loop=None,
):
    """Instantiate the configured cluster store.

    Args:
        backend: "sqlite" (default, local/dev) or "postgres".
        db_path: SQLite file path (or ":memory:") when backend == "sqlite".
        dsn: Explicit Postgres DSN — tests only; production uses env.
        dsn_env: Name of the env var carrying the DSN (default
            HERMES_CLUSTER_PG_DSN, falling back to DATABASE_URL).
        loop: Running asyncio loop when constructing the Postgres store.

    Returns:
        ClusterStore (SQLite) or PostgresClusterStore (async).
    """
    backend = (backend or "sqlite").strip().lower()

    if backend in ("postgres", "postgresql", "asyncpg"):
        resolved = dsn or os.environ.get(dsn_env, "") or os.environ.get("DATABASE_URL", "")
        if not resolved:
            raise RuntimeError(
                f"store.backend=postgres but no DSN found "
                f"(set the {dsn_env} or DATABASE_URL environment variable)"
            )
        from .postgres_store import SyncPostgresStore

        # Deliberately do NOT include the DSN in this message — it embeds
        # the DB password. Log only that the env var supplied it.
        logger.info("cluster state: Postgres store (DSN from env, backend=postgres)")
        return SyncPostgresStore(dsn=resolved)

    if backend not in ("sqlite", ""):
        raise ValueError(f"unknown store backend: {backend!r} (expected sqlite|postgres)")

    if not db_path:
        return None  # caller falls back to the in-memory ClusterState
    from .cluster_store import ClusterStore

    logger.info("cluster state: SQLite store at %s", db_path)
    return ClusterStore(db_path=db_path)
