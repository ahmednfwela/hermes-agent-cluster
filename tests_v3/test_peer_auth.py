"""Tests for peer-token HMAC-SHA256 authentication."""

import time
import pytest
from httpx import AsyncClient, ASGITransport

from hermes_cluster.app import create_app
from hermes_cluster.core import peer_auth


@pytest.fixture(autouse=True)
def reset_peer_auth():
    """Reset peer auth state between tests."""
    peer_auth.configure(local_node_id="", local_token="", peer_tokens={})
    yield
    peer_auth.configure(local_node_id="", local_token="", peer_tokens={})


class TestPeerAuthUnit:
    """Unit tests for sign_request / verify_request."""

    def test_sign_and_verify_roundtrip(self):
        peer_auth.configure(
            local_node_id="node_a",
            local_token="shared_secret_alpha",
            peer_tokens={"node_a": "shared_secret_alpha"},
        )
        body = b'{"title": "test task"}'
        headers = peer_auth.sign_request("POST", "/api/v1/federation/tasks", body)
        assert headers["X-Peer-Node"] == "node_a"
        assert headers["X-Peer-Timestamp"]
        assert headers["X-Peer-Signature"]

        ok, err = peer_auth.verify_request(
            method="POST",
            path="/api/v1/federation/tasks",
            body=body,
            headers=headers,
        )
        assert ok is True
        assert err == ""

    def test_verify_rejects_unknown_peer(self):
        peer_auth.configure(
            local_node_id="node_a",
            local_token="secret",
            peer_tokens={"node_a": "secret"},
        )
        headers = {
            "X-Peer-Node": "unknown_node",
            "X-Peer-Timestamp": str(int(time.time())),
            "X-Peer-Signature": "deadbeef",
        }
        ok, err = peer_auth.verify_request("POST", "/path", b"", headers)
        assert ok is False
        assert "unknown peer" in err

    def test_verify_rejects_stale_timestamp(self):
        peer_auth.configure(
            local_node_id="node_a",
            local_token="secret",
            peer_tokens={"node_a": "secret"},
            replay_window=60,
        )
        headers = peer_auth.sign_request(
            "POST", "/path", b"",
            timestamp=int(time.time()) - 120,  # 2 minutes ago
        )
        ok, err = peer_auth.verify_request("POST", "/path", b"", headers)
        assert ok is False
        assert "timestamp outside window" in err

    def test_verify_rejects_bad_signature(self):
        peer_auth.configure(
            local_node_id="node_a",
            local_token="secret",
            peer_tokens={"node_a": "secret"},
        )
        headers = {
            "X-Peer-Node": "node_a",
            "X-Peer-Timestamp": str(int(time.time())),
            "X-Peer-Signature": "deadbeef",
        }
        ok, err = peer_auth.verify_request("POST", "/path", b"body", headers)
        assert ok is False
        assert "signature mismatch" in err

    def test_verify_rejects_missing_headers(self):
        peer_auth.configure(
            local_node_id="node_a",
            local_token="secret",
            peer_tokens={"node_a": "secret"},
        )
        ok, err = peer_auth.verify_request("POST", "/path", b"", {})
        assert ok is False
        assert "missing" in err


class TestPeerAuthIntegration:
    """Integration tests: middleware protects federation endpoints."""

    @pytest.fixture
    def app_with_auth(self):
        import os
        os.environ["PEER_TOKEN"] = "local_secret"
        os.environ["PEER_TOKENS"] = "peer_node:peer_secret"
        peer_auth.configure(
            local_node_id="local_node",
            local_token="local_secret",
            peer_tokens={"peer_node": "peer_secret"},
        )
        app = create_app(cluster_id="test", node_id="local_node", node_role="main")
        yield app
        os.environ.pop("PEER_TOKEN", None)
        os.environ.pop("PEER_TOKENS", None)

    @pytest.fixture
    def app_no_auth(self):
        import os
        os.environ.pop("PEER_TOKEN", None)
        os.environ.pop("PEER_TOKENS", None)
        return create_app(cluster_id="test", node_id="local_node", node_role="main")

    @pytest.mark.asyncio
    async def test_federation_blocked_without_auth(self, app_with_auth):
        transport = ASGITransport(app=app_with_auth)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/federation/clusters",
                json={"name": "remote", "endpoint": "http://remote:8787"},
            )
            assert resp.status_code == 401
            assert resp.json()["error"] == "peer_auth_failed"

    @pytest.mark.asyncio
    async def test_federation_allowed_with_valid_auth(self, app_with_auth):
        peer_auth.configure(
            local_node_id="peer_node",
            local_token="peer_secret",
            peer_tokens={"peer_node": "peer_secret"},
        )
        body = b'{"name": "remote", "endpoint": "http://remote:8787"}'
        headers = peer_auth.sign_request(
            "POST", "/api/v1/federation/clusters", body,
        )
        headers["Content-Type"] = "application/json"
        transport = ASGITransport(app=app_with_auth)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/federation/clusters",
                content=body,
                headers=headers,
            )
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_non_federation_passes_without_auth(self, app_with_auth):
        transport = ASGITransport(app=app_with_auth)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_auth_configured_means_no_protection(self, app_no_auth):
        transport = ASGITransport(app=app_no_auth)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/federation/clusters",
                json={"name": "remote", "endpoint": "http://remote:8787"},
            )
            assert resp.status_code == 200
