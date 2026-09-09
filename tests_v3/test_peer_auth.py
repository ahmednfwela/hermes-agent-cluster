"""Tests for peer-token HMAC-SHA256 authentication."""

import json
import os
import subprocess
import sys
import time
import pytest
from httpx import AsyncClient, ASGITransport

from hermes_cluster.app import create_app
from hermes_cluster.core import peer_auth
from hermes_cluster.core.peer_auth import PeerAuthState


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
        # L1 fix: uniform error detail, no node-id enumeration
        assert err == "peer_auth_failed"

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
        # L1 fix: uniform error
        assert err == "peer_auth_failed"

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
        assert err == "peer_auth_failed"

    def test_verify_rejects_missing_headers(self):
        peer_auth.configure(
            local_node_id="node_a",
            local_token="secret",
            peer_tokens={"node_a": "secret"},
        )
        ok, err = peer_auth.verify_request("POST", "/path", b"", {})
        assert ok is False
        assert err == "peer_auth_failed"

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
        assert err == "peer_auth_failed"

    def test_is_configured(self):
        """L5: is_configured() accessor."""
        assert peer_auth.is_configured() is False
        peer_auth.configure(
            local_node_id="node_a",
            local_token="secret",
            peer_tokens={"node_b": "peer_secret"},
        )
        assert peer_auth.is_configured() is True

    def test_sign_returns_empty_when_not_configured(self):
        """sign_request returns empty dict when not configured."""
        headers = peer_auth.sign_request("POST", "/path", b"")
        assert headers == {}

    def test_query_string_in_signature_m1(self):
        """M1: query string IS part of the signed material."""
        peer_auth.configure(
            local_node_id="node_a",
            local_token="secret",
            peer_tokens={"node_a": "secret"},
        )
        body = b""
        # Sign with query string
        headers_with_qs = peer_auth.sign_request(
            "GET", "/api/v1/tasks?page=1", body,
        )
        # Sign without query string
        headers_no_qs = peer_auth.sign_request(
            "GET", "/api/v1/tasks", body,
        )
        # Signatures MUST differ — query string is bound
        assert headers_with_qs["X-Peer-Signature"] != headers_no_qs["X-Peer-Signature"]

        # Verify: sig over /api/v1/tasks must NOT verify for /api/v1/tasks?injected=1
        ok, _ = peer_auth.verify_request(
            "GET", "/api/v1/tasks?injected=1", body, headers_no_qs,
        )
        assert ok is False

        # Verify: sig over /api/v1/tasks?page=1 DOES verify for same path
        ok, _ = peer_auth.verify_request(
            "GET", "/api/v1/tasks?page=1", body, headers_with_qs,
        )
        assert ok is True

    def test_build_signed_path(self):
        """M1: build_signed_path extracts path?query correctly."""
        from hermes_cluster.core.peer_auth import build_signed_path
        assert build_signed_path("http://host:8787/api/v1/tasks") == "/api/v1/tasks"
        assert build_signed_path("http://host:8787/api/v1/tasks?page=1") == "/api/v1/tasks?page=1"
        assert build_signed_path("/api/v1/tasks?node=x&status=ready") == "/api/v1/tasks?node=x&status=ready"


class TestPeerAuthPerAppState:
    """H2: per-app PeerAuthState isolation — two apps in one process."""

    def test_two_apps_isolated_trust_domains(self):
        """Two PeerAuthState instances with different peer maps reject each other's peers."""
        # App 1: knows about peer_a
        state1 = PeerAuthState(
            local_node_id="app1",
            local_token="app1_secret",
            peer_tokens={"peer_a": "token_a"},
        )
        # App 2: knows about peer_b
        state2 = PeerAuthState(
            local_node_id="app2",
            local_token="app2_secret",
            peer_tokens={"peer_b": "token_b"},
        )

        # Sign as peer_a using peer_a's token
        body = b"test"
        headers = PeerAuthState(
            local_node_id="peer_a",
            local_token="token_a",
        ).sign_request("POST", "/api/v1/tasks", body)

        # App 1 accepts (it knows peer_a)
        ok, _ = state1.verify_request("POST", "/api/v1/tasks", body, headers)
        assert ok is True

        # App 2 rejects (it does NOT know peer_a) — H2 isolation
        ok, err = state2.verify_request("POST", "/api/v1/tasks", body, headers)
        assert ok is False
        assert err == "peer_auth_failed"

        # Sign as peer_b
        headers_b = PeerAuthState(
            local_node_id="peer_b",
            local_token="token_b",
        ).sign_request("POST", "/api/v1/tasks", body)

        # App 2 accepts
        ok, _ = state2.verify_request("POST", "/api/v1/tasks", body, headers_b)
        assert ok is True

        # App 1 rejects — H2 isolation
        ok, _ = state1.verify_request("POST", "/api/v1/tasks", body, headers_b)
        assert ok is False


class TestPeerAuthIntegration:
    """Integration tests: middleware protects endpoints."""

    @pytest.fixture
    def app_with_auth(self):
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
            # L1 fix: detail is uniform
            assert resp.json()["detail"] == "peer_auth_failed"

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


class TestDenyByDefault:
    """M2: deny-by-default — only public paths pass without auth."""

    @pytest.fixture
    def app_with_auth(self):
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

    @pytest.mark.asyncio
    async def test_public_paths_pass(self, app_with_auth):
        """Public paths (health, metrics, docs, etc.) pass without auth."""
        transport = ASGITransport(app=app_with_auth)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/metrics")).status_code == 200
            assert (await client.get("/docs")).status_code == 200
            assert (await client.get("/redoc")).status_code == 200
            assert (await client.get("/openapi.json")).status_code == 200

    @pytest.mark.asyncio
    async def test_config_endpoint_gated_h1(self, app_with_auth):
        """H1: /api/v1/config is gated (was previously open)."""
        transport = ASGITransport(app=app_with_auth)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/config")
            assert resp.status_code == 401
            resp = await client.put("/api/v1/config", json={})
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_nodes_endpoint_gated(self, app_with_auth):
        """Non-public endpoints are gated."""
        transport = ASGITransport(app=app_with_auth)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/nodes")
            assert resp.status_code == 401
            resp = await client.get("/api/v1/leases")
            assert resp.status_code == 401
            resp = await client.get("/api/v1/summary")
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_hooks_endpoint_gated(self, app_with_auth):
        """H1: /api/v1/hooks is gated (SSRF surface)."""
        transport = ASGITransport(app=app_with_auth)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/v1/hooks", json={"url": "http://evil.com"})
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_node_capabilities_gated(self, app_with_auth):
        """PATCH /api/v1/nodes/{id}/capabilities is gated."""
        transport = ASGITransport(app=app_with_auth)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch(
                "/api/v1/nodes/some_node/capabilities",
                json={"capabilities": ["coding"]},
            )
            assert resp.status_code == 401


class TestConfigTokenRedaction:
    """H1: token fields are redacted from GET /api/v1/config."""

    @pytest.mark.asyncio
    async def test_config_get_redacts_token(self):
        """GET /api/v1/config redacts token fields — proved by injecting a sentinel."""
        os.environ.pop("PEER_TOKEN", None)
        os.environ.pop("PEER_TOKENS", None)
        app = create_app(cluster_id="test", node_id="local_node", node_role="main")
        # Inject a sentinel token into the state's config
        sentinel = "sentinel_secret_4f6a7b8c9d0e"
        from hermes_cluster.routers import config as config_mod
        cfg = config_mod._current_config()
        cfg["cluster"]["token"] = sentinel
        if config_mod._store:
            config_mod._store.set_config(cfg)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/config")
            assert resp.status_code == 200
            resp_cfg = resp.json()
            # The sentinel MUST NOT appear in the response
            resp_text = json.dumps(resp_cfg)
            assert sentinel not in resp_text, f"Sentinel token leaked in response: {resp_text[:200]}"
            # The token field must be redacted
            assert resp_cfg.get("cluster", {}).get("token") == "***REDACTED***"

    @pytest.mark.asyncio
    async def test_config_yaml_redacts_token(self):
        """GET /api/v1/config/yaml redacts token fields."""
        os.environ.pop("PEER_TOKEN", None)
        os.environ.pop("PEER_TOKENS", None)
        app = create_app(cluster_id="test", node_id="local_node", node_role="main")
        sentinel = "sentinel_yaml_secret_a1b2c3"
        from hermes_cluster.routers import config as config_mod
        cfg = config_mod._current_config()
        cfg["cluster"]["token"] = sentinel
        if config_mod._store:
            config_mod._store.set_config(cfg)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/config/yaml")
            assert resp.status_code == 200
            yaml_str = resp.text
            assert sentinel not in yaml_str, f"Sentinel leaked in YAML: {yaml_str[:200]}"


def _assert_no_plaintext_tokens(cfg, known_sentinels=None):
    """Recursively check no plaintext token values in config."""
    if isinstance(cfg, dict):
        for key, value in cfg.items():
            if key in ("token", "secret", "password"):
                assert value == "***REDACTED***" or value == "" or value is None, \
                    f"Token field '{key}' not redacted: got value of type {type(value).__name__}"
            else:
                _assert_no_plaintext_tokens(value, known_sentinels)
    elif isinstance(cfg, list):
        for item in cfg:
            _assert_no_plaintext_tokens(item, known_sentinels)


class TestPeerAuthTwoProcessRoundTrip:
    """D2: Real two-process round-trip test.

    Spawns a server in a subprocess, then signs requests from THIS process
    and sends them to the subprocess server. Proves the signer works across
    a genuine process boundary.
    """

    @pytest.mark.asyncio
    async def test_two_process_signed_request_accepted(self):
        """Node A (this process) signs → Node B (subprocess server) accepts."""
        import socket

        # Find a free port
        sock = socket.socket()
        sock.bind(("", 0))
        port = sock.getsockname()[1]
        sock.close()

        # Start server in subprocess
        env = os.environ.copy()
        env["PEER_TOKEN"] = "server_secret_token"
        env["PEER_TOKENS"] = "client_node:client_secret_token"
        env["HERMES_TEST_NODE_ID"] = "server_node"

        server_proc = subprocess.Popen(
            [sys.executable, "-m", "hermes_cluster.serve",
             "--host", "127.0.0.1", "--port", str(port),
             "--node-id", "server_node"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            # Wait for server to be ready
            import urllib.request
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                    break
                except Exception:
                    time.sleep(0.2)
            else:
                server_proc.kill()
                stdout, stderr = server_proc.communicate(timeout=2)
                pytest.fail(f"Server didn't start. stderr: {stderr.decode()[-500:]}")

            # Sign a request from THIS process as client_node
            client_state = PeerAuthState(
                local_node_id="client_node",
                local_token="client_secret_token",
            )
            body = b'{"title": "cross-process task"}'
            headers = client_state.sign_request("POST", "/api/v1/tasks", body)
            headers["Content-Type"] = "application/json"

            # Send to server subprocess
            import urllib.request as urlreq
            req = urlreq.Request(
                f"http://127.0.0.1:{port}/api/v1/tasks",
                data=body,
                method="POST",
                headers=headers,
            )
            resp = urlreq.urlopen(req, timeout=5)
            assert resp.status == 200
            result = json.loads(resp.read().decode())
            assert "task_id" in result or "id" in result

        finally:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                server_proc.kill()

    @pytest.mark.asyncio
    async def test_two_process_unsigned_request_rejected(self):
        """Unsigned request to auth-ON subprocess server is rejected with 401."""
        import socket

        sock = socket.socket()
        sock.bind(("", 0))
        port = sock.getsockname()[1]
        sock.close()

        env = os.environ.copy()
        env["PEER_TOKEN"] = "server_secret_token"
        env["PEER_TOKENS"] = "client_node:client_secret_token"

        server_proc = subprocess.Popen(
            [sys.executable, "-m", "hermes_cluster.serve",
             "--host", "127.0.0.1", "--port", str(port),
             "--node-id", "server_node"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            # Wait for server
            import urllib.request
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                    break
                except Exception:
                    time.sleep(0.2)
            else:
                server_proc.kill()
                pytest.fail("Server didn't start")

            # Send UNSIGNED request
            import urllib.request as urlreq
            from urllib.error import HTTPError
            req = urlreq.Request(
                f"http://127.0.0.1:{port}/api/v1/tasks",
                data=b'{"title": "unsigned"}',
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with pytest.raises(HTTPError) as exc_info:
                urlreq.urlopen(req, timeout=5)
            assert exc_info.value.code == 401

        finally:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                server_proc.kill()


class TestCrossNodeProtection:
    """D2: cross-node endpoints are protected when auth is ON."""

    @pytest.fixture
    def app_with_auth(self):
        os.environ["PEER_TOKEN"] = "node_b_secret"
        os.environ["PEER_TOKENS"] = "node_a:node_a_secret"
        peer_auth.configure(
            local_node_id="node_b",
            local_token="node_b_secret",
            peer_tokens={"node_a": "node_a_secret"},
        )
        app = create_app(cluster_id="cluster_b", node_id="node_b", node_role="main")
        yield app
        os.environ.pop("PEER_TOKEN", None)
        os.environ.pop("PEER_TOKENS", None)

    @pytest.mark.asyncio
    async def test_tasks_protected(self, app_with_auth):
        transport = ASGITransport(app=app_with_auth)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post("/api/v1/tasks", json={"title": "x"})).status_code == 401
            assert (await client.get("/api/v1/tasks")).status_code == 401

    @pytest.mark.asyncio
    async def test_sync_protected(self, app_with_auth):
        transport = ASGITransport(app=app_with_auth)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/v1/sync/receive", json={"task_id": "t1"})
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_heartbeat_protected(self, app_with_auth):
        transport = ASGITransport(app=app_with_auth)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/v1/nodes/heartbeat", json={"node_id": "x"})
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_signed_accepted(self, app_with_auth):
        """Signed request is accepted."""
        peer_auth.configure(
            local_node_id="node_a",
            local_token="node_a_secret",
            peer_tokens={"node_b": "node_b_secret"},
        )
        body = b'{"title": "signed task"}'
        headers = peer_auth.sign_request("POST", "/api/v1/tasks", body)
        headers["Content-Type"] = "application/json"
        # Restore server's state for verification
        peer_auth.configure(
            local_node_id="node_b",
            local_token="node_b_secret",
            peer_tokens={"node_a": "node_a_secret"},
        )
        transport = ASGITransport(app=app_with_auth)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/v1/tasks", content=body, headers=headers)
            assert resp.status_code == 200


class TestWebhookPublicWithAuthOn:
    """Webhook must work with peer auth ON — it has its own X-Gitlab-Token auth.

    GitLab (external) can never sign peer-HMAC, so the webhook path is in the
    PUBLIC set. Its auth is the X-Gitlab-Token header (from PR#2).
    """

    @pytest.fixture
    def app_with_auth_and_webhook_secret(self):
        os.environ["PEER_TOKEN"] = "local_secret"
        os.environ["PEER_TOKENS"] = "peer_node:peer_secret"
        os.environ["GITLAB_INTAKE_WEBHOOK_SECRET"] = "webhook_secret_xyz"
        peer_auth.configure(
            local_node_id="local_node",
            local_token="local_secret",
            peer_tokens={"peer_node": "peer_secret"},
        )
        app = create_app(cluster_id="test", node_id="local_node", node_role="main")
        yield app
        os.environ.pop("PEER_TOKEN", None)
        os.environ.pop("PEER_TOKENS", None)
        os.environ.pop("GITLAB_INTAKE_WEBHOOK_SECRET", None)

    @pytest.mark.asyncio
    async def test_webhook_passes_peer_auth_with_correct_gitlab_token(self, app_with_auth_and_webhook_secret):
        """Correct X-Gitlab-Token → webhook reaches handler (not 401 from peer-auth)."""
        transport = ASGITransport(app=app_with_auth_and_webhook_secret)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Non-issue event → ignored by handler, but NOT 401 from peer-auth
            resp = await client.post(
                "/api/v1/intake/gitlab/webhook",
                json={"object_kind": "push"},
                headers={"X-Gitlab-Token": "webhook_secret_xyz"},
            )
            # Handler returns 200 with "ignored" — NOT 401
            assert resp.status_code == 200
            assert resp.json()["status"] == "ignored"

    @pytest.mark.asyncio
    async def test_webhook_rejects_wrong_gitlab_token(self, app_with_auth_and_webhook_secret):
        """Wrong X-Gitlab-Token → 401 from the intake handler's own auth check."""
        transport = ASGITransport(app=app_with_auth_and_webhook_secret)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/intake/gitlab/webhook",
                json={"object_kind": "issue"},
                headers={"X-Gitlab-Token": "wrong_token"},
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_webhook_rejects_missing_gitlab_token(self, app_with_auth_and_webhook_secret):
        """Missing X-Gitlab-Token → 401 from the intake handler's own auth check."""
        transport = ASGITransport(app=app_with_auth_and_webhook_secret)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/intake/gitlab/webhook",
                json={"object_kind": "issue"},
            )
            assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_webhook_creates_task_with_valid_token(self, app_with_auth_and_webhook_secret):
        """Correct token + valid issue payload → task created (proves full round-trip)."""
        transport = ASGITransport(app=app_with_auth_and_webhook_secret)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/intake/gitlab/webhook",
                json={
                    "object_kind": "issue",
                    "object_attributes": {
                        "iid": 999,
                        "action": "open",
                        "title": "Test webhook task",
                    },
                    "labels": [{"title": "tooling"}],
                },
                headers={"X-Gitlab-Token": "webhook_secret_xyz"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "created"
            assert "task_id" in data


class TestConfigDeepCopyNoMutation:
    """N3a/N3b: GET /api/v1/config must NOT mutate the live store via redaction."""

    @pytest.mark.asyncio
    async def test_get_config_does_not_corrupt_store(self):
        """After GET /api/v1/config, the store still holds the real token."""
        os.environ.pop("PEER_TOKEN", None)
        os.environ.pop("PEER_TOKENS", None)
        app = create_app(cluster_id="test", node_id="local_node", node_role="main")
        sentinel = "sentinel_deep_copy_test_abc123"
        from hermes_cluster.routers import config as config_mod
        cfg = config_mod._current_config()
        cfg["cluster"]["token"] = sentinel
        if config_mod._store:
            config_mod._store.set_config(cfg)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # First GET — redacts in response
            resp1 = await client.get("/api/v1/config")
            assert resp1.status_code == 200
            assert sentinel not in json.dumps(resp1.json())

            # Store must STILL hold the real sentinel (not ***REDACTED***)
            live = config_mod._current_config()
            assert live["cluster"]["token"] == sentinel, \
                "GET /api/v1/config mutated the live store — N3a regression"

            # Second GET — still redacted (not double-redacted)
            resp2 = await client.get("/api/v1/config")
            assert resp2.json()["cluster"]["token"] == "***REDACTED***"

    @pytest.mark.asyncio
    async def test_validate_does_not_leak_token(self):
        """POST /api/v1/config/validate (no body) must NOT echo raw tokens."""
        os.environ.pop("PEER_TOKEN", None)
        os.environ.pop("PEER_TOKENS", None)
        app = create_app(cluster_id="test", node_id="local_node", node_role="main")
        sentinel = "sentinel_validate_leak_def456"
        from hermes_cluster.routers import config as config_mod
        cfg = config_mod._current_config()
        cfg["cluster"]["token"] = sentinel
        if config_mod._store:
            config_mod._store.set_config(cfg)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/v1/config/validate")
            assert resp.status_code == 200
            resp_text = json.dumps(resp.json())
            assert sentinel not in resp_text, \
                f"POST /config/validate leaked sentinel: {resp_text[:200]}"
            # Config echo must be redacted
            assert resp.json()["config"]["cluster"]["token"] == "***REDACTED***"

    @pytest.mark.asyncio
    async def test_federation_token_also_redacted(self):
        """federation.token is also a sensitive field — must be redacted."""
        os.environ.pop("PEER_TOKEN", None)
        os.environ.pop("PEER_TOKENS", None)
        app = create_app(cluster_id="test", node_id="local_node", node_role="main")
        sentinel = "sentinel_federation_token_ghi789"
        from hermes_cluster.routers import config as config_mod
        cfg = config_mod._current_config()
        cfg.setdefault("federation", {})["token"] = sentinel
        if config_mod._store:
            config_mod._store.set_config(cfg)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/config")
            assert resp.status_code == 200
            resp_text = json.dumps(resp.json())
            assert sentinel not in resp_text, \
                f"federation.token leaked in GET /config: {resp_text[:200]}"
