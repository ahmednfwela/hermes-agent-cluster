"""store.db_path makes the main's task queue survive a restart (in-memory default unchanged)."""
import os
from fastapi.testclient import TestClient
from hermes_cluster.app import create_app
from hermes_cluster.state import ClusterState
from hermes_cluster.state.cluster_store import ClusterStore


def _no_auth(monkeypatch):
    monkeypatch.delenv("PEER_TOKEN", raising=False)
    monkeypatch.delenv("PEER_TOKENS", raising=False)


def test_default_is_in_memory(monkeypatch):
    _no_auth(monkeypatch)
    app = create_app(cluster_id="c", node_id="m", node_role="main")
    from hermes_cluster.routers import tasks as tasks_mod
    assert isinstance(tasks_mod._state if hasattr(tasks_mod, "_state") else tasks_mod.state, ClusterState)


def test_tasks_survive_restart_with_db_path(tmp_path, monkeypatch):
    _no_auth(monkeypatch)
    db = str(tmp_path / "cluster.db")
    app1 = create_app(cluster_id="c", node_id="m", node_role="main", db_path=db)
    with TestClient(app1) as c1:
        r = c1.post("/api/v1/tasks", json={"title": "persist me", "requires": [], "priority": 3})
        assert r.status_code == 200, r.text
        tid = r.json()["id"]
    # "restart": a fresh app on the same file
    app2 = create_app(cluster_id="c", node_id="m", node_role="main", db_path=db)
    with TestClient(app2) as c2:
        items = c2.get("/api/v1/tasks").json()
        items = items if isinstance(items, list) else items.get("tasks", [])
        assert any(t["id"] == tid for t in items), "task lost across restart"


def test_db_path_selects_sqlite_store(tmp_path, monkeypatch):
    _no_auth(monkeypatch)
    db = str(tmp_path / "s.db")
    create_app(cluster_id="c", node_id="m", node_role="main", db_path=db)
    from hermes_cluster.routers import tasks as tasks_mod
    st = tasks_mod._state if hasattr(tasks_mod, "_state") else tasks_mod.state
    assert isinstance(st, ClusterStore)
    assert os.path.exists(db)
