"""Tests for GitLab intake router."""

import os
import pytest
from httpx import AsyncClient, ASGITransport
from hermes_cluster.app import create_app
from hermes_cluster.state import ClusterState


@pytest.fixture
def app():
    os.environ.pop("GITLAB_INTAKE_TOKEN", None)
    return create_app(cluster_id="test", node_id="test-node", node_role="main")


@pytest.mark.asyncio
async def test_webhook_creates_task(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "object_kind": "issue",
            "object_attributes": {
                "iid": 999,
                "title": "Test intake issue",
                "url": "https://gitlab.bdaya-dev.com/shared/claude-plugins/-/issues/999",
                "action": "open",
            },
            "labels": [{"title": "tooling"}],
        }
        resp = await client.post("/api/v1/intake/gitlab/webhook", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "created"
        assert body["task_id"].startswith("task_")
        assert body["task"]["title"] == "[#999] Test intake issue"
        assert "tooling" in body["task"]["requires"]


@pytest.mark.asyncio
async def test_webhook_idempotent(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "object_kind": "issue",
            "object_attributes": {
                "iid": 888,
                "title": "Same issue",
                "url": "https://gitlab.bdaya-dev.com/shared/claude-plugins/-/issues/888",
                "action": "open",
            },
            "labels": [{"title": "tooling"}],
        }
        r1 = await client.post("/api/v1/intake/gitlab/webhook", json=payload)
        r2 = await client.post("/api/v1/intake/gitlab/webhook", json=payload)
        assert r1.json()["task_id"] == r2.json()["task_id"]


@pytest.mark.asyncio
async def test_webhook_ignores_wrong_label(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "object_kind": "issue",
            "object_attributes": {"iid": 777, "title": "Nope", "action": "open"},
            "labels": [{"title": "bug"}],
        }
        resp = await client.post("/api/v1/intake/gitlab/webhook", json=payload)
        assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_webhook_ignores_non_issue_event(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/intake/gitlab/webhook", json={"object_kind": "merge_request"})
        assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_status_unconfigured(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/intake/gitlab/status")
        assert resp.status_code == 200
        assert resp.json()["configured"] is False
