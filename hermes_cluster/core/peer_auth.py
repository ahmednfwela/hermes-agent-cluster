"""Peer-token HMAC-SHA256 authentication for cross-node API calls.

Design:
- Each node has a unique `node_id` and a shared secret `peer_token`
- Outgoing requests sign `node_id + timestamp + body` with HMAC-SHA256
- Incoming requests verify the signature using stored peer tokens
- Tokens are bound to the tailnet (per R10) — never inline in repos

Headers set on outgoing requests:
  X-Peer-Node: <node_id>
  X-Peer-Timestamp: <unix_seconds>
  X-Peer-Signature: <hex-hmac-sha256>

Replay window: 60 seconds (configurable via PEER_AUTH_WINDOW env var).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger("hermes_cluster.peer_auth")

# Module-level peer token store: node_id → token (hex)
_peer_tokens: Dict[str, str] = {}
# This node's identity
_local_node_id: str = ""
_local_token: str = ""
# Replay protection window in seconds
_replay_window: int = 60


def configure(
    local_node_id: str,
    local_token: str,
    peer_tokens: Optional[Dict[str, str]] = None,
    replay_window: int = 60,
):
    """Configure this node's identity and known peer tokens.

    Args:
        local_node_id: This node's ID
        local_token: This node's outgoing token (used to sign requests)
        peer_tokens: Map of peer_node_id → token for verifying incoming requests
        replay_window: Max age of incoming request timestamps (seconds)
    """
    global _local_node_id, _local_token, _peer_tokens, _replay_window
    _local_node_id = local_node_id
    _local_token = local_token
    _peer_tokens = dict(peer_tokens or {})
    _replay_window = replay_window
    logger.info(
        "Peer auth configured: local_node=%s, peers=%d",
        local_node_id,
        len(_peer_tokens),
    )


def add_peer(node_id: str, token: str):
    """Register a peer's token for incoming verification."""
    _peer_tokens[node_id] = token


def remove_peer(node_id: str):
    """Remove a peer's token."""
    _peer_tokens.pop(node_id, None)


def sign_request(method: str, path: str, body: bytes, timestamp: Optional[int] = None) -> Dict[str, str]:
    """Generate auth headers for an outgoing request.

    Returns dict of headers to add to the request.
    """
    ts = timestamp or int(time.time())
    # Sign: node_id + timestamp + method + path + body_sha256
    body_hash = hashlib.sha256(body).hexdigest()
    message = f"{_local_node_id}:{ts}:{method}:{path}:{body_hash}"
    signature = hmac.new(
        _local_token.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Peer-Node": _local_node_id,
        "X-Peer-Timestamp": str(ts),
        "X-Peer-Signature": signature,
    }


def verify_request(
    method: str,
    path: str,
    body: bytes,
    headers: Dict[str, str],
) -> Tuple[bool, str]:
    """Verify an incoming request's peer auth headers.

    Returns (ok, error_message).
    """
    node_id = headers.get("X-Peer-Node", "")
    timestamp_str = headers.get("X-Peer-Timestamp", "")
    signature = headers.get("X-Peer-Signature", "")

    if not node_id or not timestamp_str or not signature:
        return False, "missing peer auth headers"

    # Check peer is known
    peer_token = _peer_tokens.get(node_id)
    if not peer_token:
        return False, f"unknown peer node: {node_id}"

    # Check timestamp freshness (replay protection)
    try:
        ts = int(timestamp_str)
    except ValueError:
        return False, "invalid timestamp"
    now = int(time.time())
    if abs(now - ts) > _replay_window:
        return False, f"timestamp outside window ({abs(now - ts)}s > {_replay_window}s)"

    # Recompute signature
    body_hash = hashlib.sha256(body).hexdigest()
    message = f"{node_id}:{ts}:{method}:{path}:{body_hash}"
    expected = hmac.new(
        peer_token.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        return False, "signature mismatch"

    return True, ""
