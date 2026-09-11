"""Backend selection & config wiring tests (#829 req. 1/3).

SQLite stays the default (local/dev); postgres is selected by config.
The literal-DSN guard and env-var-only DSN resolution are tested here —
none of these tests need a reachable Postgres server.
"""

from __future__ import annotations

import pytest

from hermes_cluster.state.cluster_store import ClusterStore
from hermes_cluster.state.factory import create_store
from hermes_cluster.state.postgres_store import SyncPostgresStore


def test_default_backend_is_sqlite(tmp_path):
    store = create_store(backend="sqlite", db_path=str(tmp_path / "x.db"))
    assert isinstance(store, ClusterStore)
    store.close()


def test_no_db_path_means_inmemory_placeholder():
    assert create_store(backend="sqlite", db_path="") is None


def test_postgres_without_dsn_raises(monkeypatch):
    monkeypatch.delenv("HERMES_CLUSTER_PG_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="no DSN"):
        create_store(backend="postgres")


def _fake_pg_dsn() -> str:
    """Assembled at runtime so no credential-shaped literal sits in the
    repo (and the leak-assertions below compare against a secret the file
    itself does not contain verbatim)."""
    return "postgresql://" + "u" + ":" + "abc" + "123" + "@127.0.0.1:1/db"


def test_postgres_dsn_from_env(monkeypatch):
    """The DSN is resolved from the named env var; a bogus host must fail
    WITHOUT echoing the secret."""
    dsn = _fake_pg_dsn()
    monkeypatch.setenv("HERMES_CLUSTER_PG_DSN", dsn)
    with pytest.raises(Exception) as exc:  # connect refused / timeout
        create_store(backend="postgres")
    assert "abc123" not in str(exc.value)
    assert "u:abc123" not in str(exc.value)


def test_unknown_backend_rejected():
    with pytest.raises(ValueError, match="unknown store backend"):
        create_store(backend="couchdb")


def test_dsn_never_logged(monkeypatch, caplog):
    """Log records on the factory/postgres loggers must not carry the secret."""
    import logging

    monkeypatch.setenv("HERMES_CLUSTER_PG_DSN", _fake_pg_dsn())
    caplog.set_level(logging.DEBUG)
    with pytest.raises(Exception):
        create_store(backend="postgres")
    assert "abc123" not in caplog.text
    assert "postgresql://" not in caplog.text


def test_facade_attribute_delegation():
    """SyncPostgresStore mirrors ClusterStore's sync attribute surface
    without needing a live connection."""
    from hermes_cluster.state.postgres_store import PostgresClusterStore

    obj = SyncPostgresStore.__new__(SyncPostgresStore)
    inner = PostgresClusterStore(dsn=_fake_pg_dsn())
    object.__setattr__(obj, "_store", inner)

    class _Bridge:
        ran = []

        def run(self, coro, timeout=120.0):
            coro.close()  # never actually awaited in this test
            return "ran"

    object.__setattr__(obj, "_bridge", _Bridge())

    obj.cluster_id = "c9"
    assert obj.cluster_id == "c9"
    assert inner.cluster_id == "c9"
    assert obj.get_config_path() == ""      # plain sync method, no bridge
    assert obj.get_summary() == "ran"       # coroutine bridged
    assert obj.node_count() == "ran"


def test_serve_config_rejects_literal_dsn(tmp_path, monkeypatch):
    """store.dsn in cluster.yaml would commit a DB password to the repo —
    serve.py must fail fast with a clear message."""
    cfg = tmp_path / "cluster.yaml"
    cfg.write_text(
        "store:\n  backend: postgres\n  dsn: 'postgresql://user-not-real@h/db'\n",
        encoding="utf-8",
    )
    from hermes_cluster import serve

    monkeypatch.setattr("sys.argv", ["serve", "--config", str(cfg)])
    with pytest.raises(SystemExit):
        serve.main()


def test_serve_config_selects_postgres_backend(tmp_path, monkeypatch):
    cfg = tmp_path / "cluster.yaml"
    cfg.write_text(
        "store:\n  backend: postgres\n  dsn_env: HERMES_CLUSTER_PG_DSN\n",
        encoding="utf-8",
    )
    from hermes_cluster import serve

    # Stop before uvicorn.run — we only assert config parsing.
    monkeypatch.setattr("sys.argv", ["serve", "--config", str(cfg)])
    seen = {}

    def _fake_create_app(**kwargs):
        seen.update(kwargs)
        raise SystemExit(99)

    monkeypatch.setattr("hermes_cluster.app.create_app", _fake_create_app)
    with pytest.raises(SystemExit) as exc:
        serve.main()
    assert exc.value.code == 99
    assert seen["store_backend"] == "postgres"
    assert seen["store_dsn_env"] == "HERMES_CLUSTER_PG_DSN"


def test_sqlite_stays_default_when_backend_unset(tmp_path, monkeypatch):
    cfg = tmp_path / "cluster.yaml"
    cfg.write_text(f"store:\n  db_path: '{(tmp_path / 'c.db').as_posix()}'\n",
                   encoding="utf-8")
    from hermes_cluster import serve

    monkeypatch.setattr("sys.argv", ["serve", "--config", str(cfg)])
    seen = {}

    def _fake_create_app(**kwargs):
        seen.update(kwargs)
        raise SystemExit(99)

    monkeypatch.setattr("hermes_cluster.app.create_app", _fake_create_app)
    with pytest.raises(SystemExit):
        serve.main()
    assert seen["store_backend"] == ""
    assert seen["db_path"]  # SQLite path honoured as before
