"""Peer-token HMAC-SHA256 authentication for cross-node API calls.

Design:
- Each node has a unique `node_id` and a shared secret `peer_token`
- Outgoing requests sign `node_id:ts:method:path?query:body_sha256` with HMAC-SHA256
- Incoming requests verify the signature using stored peer tokens
- Tokens are bound to the tailnet (per R10) — never inline in repos

Headers set on outgoing requests:
  X-Peer-Node: <node_id>
  X-Peer-Timestamp: <unix_seconds>
  X-Peer-Signature: <hex-hmac-sha256>

Replay window: 60 seconds (configurable via PEER_AUTH_WINDOW env var).
NOTE: replay is unlimited within the window — there is no nonce/seen-cache.
Timestamp + body hash bind each signature to a specific (method, path, body) tuple,
but a captured request can be replayed verbatim until the timestamp expires.
Tracked as bdaya-defer — nonces require a shared seen-cache which is out of scope
for P1.2 (tailnet-bound tokens per R10 mitigate on-path capture).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger("hermes_cluster.peer_auth")


class PeerAuthState:
    """Per-app peer auth state — binds token store to the app/middleware instance (H2 fix).

    Two apps in one process can each have their own PeerAuthState with different
    peer maps, so they don't share a trust domain.
    """

    def __init__(
        self,
        local_node_id: str = "",
        local_token: str = "",
        peer_tokens: Optional[Dict[str, str]] = None,
        replay_window: int = 60,
    ):
        self.local_node_id = local_node_id
        self.local_token = local_token
        self.peer_tokens: Dict[str, str] = dict(peer_tokens or {})
        self.replay_window = replay_window

    def is_configured(self) -> bool:
        """Return True if this node has a token and at least one peer configured."""
        return bool(self.local_token) and bool(self.local_node_id)

    def add_peer(self, node_id: str, token: str):
        """Register a peer's token for incoming verification."""
        self.peer_tokens[node_id] = token

    def remove_peer(self, node_id: str):
        """Remove a peer's token."""
        self.peer_tokens.pop(node_id, None)

    def sign_request(
        self, method: str, path: str, body: bytes, timestamp: Optional[int] = None
    ) -> Dict[str, str]:
        """Generate auth headers for an outgoing request.

        Signs: node_id:ts:method:path?query:body_sha256
        Query string IS included in the signed material (M1 fix).

        Returns dict of headers to add to the request.
        Returns empty dict if not configured.
        """
        if not self.is_configured():
            return {}
        ts = timestamp or int(time.time())
        body_hash = hashlib.sha256(body).hexdigest()
        message = f"{self.local_node_id}:{ts}:{method}:{path}:{body_hash}"
        signature = hmac.new(
            self.local_token.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "X-Peer-Node": self.local_node_id,
            "X-Peer-Timestamp": str(ts),
            "X-Peer-Signature": signature,
        }

    def verify_request(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: Dict[str, str],
    ) -> Tuple[bool, str]:
        """Verify an incoming request's peer auth headers.

        Returns (ok, error_message). Error message is always a uniform
        'peer_auth_failed' string to avoid node-id enumeration (L1 fix).
        Specifics are logged server-side.
        """
        node_id = headers.get("X-Peer-Node", "")
        timestamp_str = headers.get("X-Peer-Timestamp", "")
        signature = headers.get("X-Peer-Signature", "")

        if not node_id or not timestamp_str or not signature:
            return False, "peer_auth_failed"

        # Check peer is known
        peer_token = self.peer_tokens.get(node_id)
        if not peer_token:
            logger.warning("Unknown peer node: %s", node_id)
            return False, "peer_auth_failed"

        # Check timestamp freshness (replay protection window)
        try:
            ts = int(timestamp_str)
        except ValueError:
            return False, "peer_auth_failed"
        now = int(time.time())
        if abs(now - ts) > self.replay_window:
            return False, "peer_auth_failed"

        # Recompute signature — path includes query string (M1 fix)
        body_hash = hashlib.sha256(body).hexdigest()
        message = f"{node_id}:{ts}:{method}:{path}:{body_hash}"
        expected = hmac.new(
            peer_token.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()

        # Compare as bytes to handle non-ASCII signatures safely (D1 fix)
        try:
            sig_bytes = signature.encode("latin-1")
            exp_bytes = expected.encode("ascii")
            if not hmac.compare_digest(exp_bytes, sig_bytes):
                return False, "peer_auth_failed"
        except (UnicodeDecodeError, UnicodeEncodeError, TypeError):
            return False, "peer_auth_failed"

        return True, ""


# ---------------------------------------------------------------------------
# Module-level default state — for backward compatibility and for callers
# that share a process with the server (e.g. plugin.py in single-process mode).
# Per-app isolation: use PeerAuthState directly (see H2 fix).
# ---------------------------------------------------------------------------
_default_state = PeerAuthState()


def configure(
    local_node_id: str,
    local_token: str,
    peer_tokens: Optional[Dict[str, str]] = None,
    replay_window: int = 60,
):
    """Configure the module-level default peer auth state."""
    global _default_state
    _default_state = PeerAuthState(
        local_node_id=local_node_id,
        local_token=local_token,
        peer_tokens=peer_tokens,
        replay_window=replay_window,
    )
    logger.info(
        "Peer auth configured: local_node=%s, peers=%d",
        local_node_id,
        len(_default_state.peer_tokens),
    )


def is_configured() -> bool:
    """Return True if the module-level default state is configured (L5 fix)."""
    return _default_state.is_configured()


def get_default_state() -> PeerAuthState:
    """Return the module-level default state."""
    return _default_state


def add_peer(node_id: str, token: str):
    """Register a peer's token on the default state."""
    _default_state.add_peer(node_id, token)


def remove_peer(node_id: str):
    """Remove a peer's token from the default state."""
    _default_state.remove_peer(node_id)


def sign_request(method: str, path: str, body: bytes, timestamp: Optional[int] = None) -> Dict[str, str]:
    """Sign using the module-level default state."""
    return _default_state.sign_request(method, path, body, timestamp)


def verify_request(
    method: str,
    path: str,
    body: bytes,
    headers: Dict[str, str],
) -> Tuple[bool, str]:
    """Verify using the module-level default state."""
    return _default_state.verify_request(method, path, body, headers)


def build_signed_path(url: str) -> str:
    """Extract path?query from a URL for signing (M1: query string inside MAC).

    Returns the path component including query string, e.g. '/api/v1/tasks?node=x'.
    If no query string, returns just the path.
    """
    parsed = urlparse(url)
    if parsed.query:
        return f"{parsed.path}?{parsed.query}"
    return parsed.path
