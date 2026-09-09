"""Tests for peer-token HMAC-SHA256 authentication."""

import time
import pytest
from httpx import AsyncClient, ASGITransport

from hermes_cluster.app import create_app
from hermes_cluster.core import peer_auth
from hermes_cluster.core.peer_client import PeerClient


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

    def test_verify_rejects_non_ascii_signature_d1(self):
        """D1: non-ASCII signature should not crash (TypeError -> 500), should return 401."""
        peer_auth.configure(
            local_node_id="node_a",
            local_token="secret",
            peer_tokens={"node_a": "secret"},
        )
        headers = {
            "X-Peer-Node": "node_a",
            "X-Peer-Timestamp": str(int(time.time())),
            "X-Peer-Signature": "非ASCII文字",  # Non-ASCII characters
        }
        ok, err = peer_auth.verify_request("POST", "/path", b"body", headers)
        assert ok is False
        assert "malformed signature" in err or "signature mismatch" in err


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

    @pytest.mark.asyncio
    async def test_malformed_signature_returns_401_not_500_d1(self, app_with_auth):
        """D1: malformed signature header should return 401, not crash with 500."""
        transport = ASGITransport(app=app_with_auth)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Malformed signature (not valid hex) - tests the error path
            resp = await client.post(
                "/api/v1/federation/clusters",
                json={"name": "remote", "endpoint": "http://remote:8787"},
                headers={
                    "X-Peer-Node": "peer_node",
                    "X-Peer-Timestamp": str(int(time.time())),
                    "X-Peer-Signature": "not-a-valid-hex-signature",
                },
            )
            assert resp.status_code == 401
            assert resp.json()["error"] == "peer_auth_failed"


class TestPeerAuthTwoProcessRoundTrip:
    """D2: Two-process round-trip test - node A signs, node B verifies."""

    @pytest.mark.asyncio
    async def test_cross_node_signed_request_accepted_d2(self):
        """Node A (client) signs request, Node B (server with auth ON) accepts it."""
        import os

        # Node B: server with peer auth enabled, knows about node_a
        os.environ["PEER_TOKEN"] = "node_b_secret"
        os.environ["PEER_TOKENS"] = "node_a:node_a_secret"
        peer_auth.configure(
            local_node_id="node_b",
            local_token="node_b_secret",
            peer_tokens={"node_a": "node_a_secret"},
        )
        app_b = create_app(cluster_id="cluster_b", node_id="node_b", node_role="main")

        # Simulate Node A signing: temporarily switch to node_a credentials
        # In reality, node A would be a separate process with its own peer_auth state
        peer_auth.configure(
            local_node_id="node_a",
            local_token="node_a_secret",
            peer_tokens={"node_b": "node_b_secret"},
        )

        # Sign the request as node_a
        body = b'{"title": "cross-node task"}'
        headers = peer_auth.sign_request("POST", "/api/v1/tasks", body)
        headers["Content-Type"] = "application/json"

        # Restore node_b state for the server to verify
        peer_auth.configure(
            local_node_id="node_b",
            local_token="node_b_secret",
            peer_tokens={"node_a": "node_a_secret"},
        )

        # Make the request to node B
        transport = ASGITransport(app=app_b)
        async with AsyncClient(transport=transport, base_url="http://node-b") as client:
            resp = await client.post(
                "/api/v1/tasks",
                content=body,
                headers=headers,
            )
            # Node B should accept the signed request from node_a
            assert resp.status_code == 200

        os.environ.pop("PEER_TOKEN", None)
        os.environ.pop("PEER_TOKENS", None)

    @pytest.mark.asyncio
    async def test_cross_node_unsigned_request_rejected_d2(self):
        """Unsigned request to auth-enabled node is rejected with 401."""
        import os

        # Node B: server with peer auth enabled
        os.environ["PEER_TOKEN"] = "node_b_secret"
        os.environ["PEER_TOKENS"] = "node_a:node_a_secret"
        peer_auth.configure(
            local_node_id="node_b",
            local_token="node_b_secret",
            peer_tokens={"node_a": "node_a_secret"},
        )
        app_b = create_app(cluster_id="cluster_b", node_id="node_b", node_role="main")

        transport = ASGITransport(app=app_b)
        async with AsyncClient(transport=transport, base_url="http://node-b") as client:
            # Unsigned request
            resp = await client.post(
                "/api/v1/tasks",
                json={"title": "unsigned task"},
            )
            # Should be rejected
            assert resp.status_code == 401
            assert resp.json()["error"] == "peer_auth_failed"

        os.environ.pop("PEER_TOKEN", None)
        os.environ.pop("PEER_TOKENS", None)

    @pytest.mark.asyncio
    async def test_cross_node_sync_endpoint_protected_d2(self):
        """D2: /api/v1/sync/receive is protected when auth is ON."""
        import os

        os.environ["PEER_TOKEN"] = "node_b_secret"
        os.environ["PEER_TOKENS"] = "node_a:node_a_secret"
        peer_auth.configure(
            local_node_id="node_b",
            local_token="node_b_secret",
            peer_tokens={"node_a": "node_a_secret"},
        )
        app_b = create_app(cluster_id="cluster_b", node_id="node_b", node_role="main")

        transport = ASGITransport(app=app_b)
        async with AsyncClient(transport=transport, base_url="http://node-b") as client:
            # Unsigned sync request should be rejected
            resp = await client.post(
                "/api/v1/sync/receive",
                json={"task_id": "task_123", "status": "completed"},
            )
            assert resp.status_code == 401

        os.environ.pop("PEER_TOKEN", None)
        os.environ.pop("PEER_TOKENS", None)

    @pytest.mark.asyncio
    async def test_cross_node_heartbeat_protected_d2(self):
        """D2: /api/v1/nodes/heartbeat is protected when auth is ON."""
        import os

        os.environ["PEER_TOKEN"] = "node_b_secret"
        os.environ["PEER_TOKENS"] = "node_a:node_a_secret"
        peer_auth.configure(
            local_node_id="node_b",
            local_token="node_b_secret",
            peer_tokens={"node_a": "node_a_secret"},
        )
        app_b = create_app(cluster_id="cluster_b", node_id="node_b", node_role="main")

        transport = ASGITransport(app=app_b)
        async with AsyncClient(transport=transport, base_url="http://node-b") as client:
            # Unsigned heartbeat should be rejected
            resp = await client.post(
                "/api/v1/nodes/heartbeat",
                json={"node_id": "node_a", "status": "online"},
            )
            assert resp.status_code == 401

        os.environ.pop("PEER_TOKEN", None)
        os.environ.pop("PEER_TOKENS", None)
