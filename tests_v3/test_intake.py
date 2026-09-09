"""Tests for GitLab intake router."""

import os
import pytest
from httpx import AsyncClient, ASGITransport
from hermes_cluster.app import create_app
from hermes_cluster.state import ClusterState


@pytest.fixture
def app():
    os.environ.pop("GITLAB_INTAKE_TOKEN", None)
    os.environ.pop("GITLAB_INTAKE_WEBHOOK_SECRET", None)
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
        assert r1.json()["status"] == "created"
        r2 = await client.post("/api/v1/intake/gitlab/webhook", json=payload)
        assert r2.json()["status"] == "deduped"
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


@pytest.mark.asyncio
async def test_webhook_secret_validation_correct():
    """Test that correct webhook secret is accepted."""
    os.environ["GITLAB_INTAKE_WEBHOOK_SECRET"] = "test-secret-123"
    try:
        app = create_app(cluster_id="test", node_id="test-node", node_role="main")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            payload = {
                "object_kind": "issue",
                "object_attributes": {
                    "iid": 111,
                    "title": "Test with secret",
                    "action": "open",
                },
                "labels": [{"title": "tooling"}],
            }
            resp = await client.post(
                "/api/v1/intake/gitlab/webhook",
                json=payload,
                headers={"X-Gitlab-Token": "test-secret-123"}
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "created"
    finally:
        os.environ.pop("GITLAB_INTAKE_WEBHOOK_SECRET", None)


@pytest.mark.asyncio
async def test_webhook_secret_validation_wrong():
    """Test that wrong webhook secret is rejected with 401."""
    os.environ["GITLAB_INTAKE_WEBHOOK_SECRET"] = "test-secret-123"
    try:
        app = create_app(cluster_id="test", node_id="test-node", node_role="main")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            payload = {
                "object_kind": "issue",
                "object_attributes": {
                    "iid": 222,
                    "title": "Test with wrong secret",
                    "action": "open",
                },
                "labels": [{"title": "tooling"}],
            }
            resp = await client.post(
                "/api/v1/intake/gitlab/webhook",
                json=payload,
                headers={"X-Gitlab-Token": "wrong-secret"}
            )
            assert resp.status_code == 401
            assert "Invalid webhook token" in resp.json()["detail"]
    finally:
        os.environ.pop("GITLAB_INTAKE_WEBHOOK_SECRET", None)


@pytest.mark.asyncio
async def test_webhook_secret_validation_missing():
    """Test that missing webhook secret is rejected with 401 when secret is configured."""
    os.environ["GITLAB_INTAKE_WEBHOOK_SECRET"] = "test-secret-123"
    try:
        app = create_app(cluster_id="test", node_id="test-node", node_role="main")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            payload = {
                "object_kind": "issue",
                "object_attributes": {
                    "iid": 333,
                    "title": "Test without secret header",
                    "action": "open",
                },
                "labels": [{"title": "tooling"}],
            }
            resp = await client.post("/api/v1/intake/gitlab/webhook", json=payload)
            assert resp.status_code == 401
            assert "Invalid webhook token" in resp.json()["detail"]
    finally:
        os.environ.pop("GITLAB_INTAKE_WEBHOOK_SECRET", None)


@pytest.mark.asyncio
async def test_webhook_iid_validation_missing(app):
    """Test that missing iid is rejected with 400."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "object_kind": "issue",
            "object_attributes": {
                "title": "Test without iid",
                "action": "open",
            },
            "labels": [{"title": "tooling"}],
        }
        resp = await client.post("/api/v1/intake/gitlab/webhook", json=payload)
        assert resp.status_code == 400
        assert "Invalid or missing iid" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_webhook_iid_validation_non_integer(app):
    """Test that non-integer iid is rejected with 400."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "object_kind": "issue",
            "object_attributes": {
                "iid": "not-an-integer",
                "title": "Test with string iid",
                "action": "open",
            },
            "labels": [{"title": "tooling"}],
        }
        resp = await client.post("/api/v1/intake/gitlab/webhook", json=payload)
        assert resp.status_code == 400
        assert "Invalid or missing iid" in resp.json()["detail"]
