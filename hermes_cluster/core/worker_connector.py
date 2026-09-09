"""Worker connector — outbound signed join+heartbeat from worker to main.

When a node runs with role=worker and cluster.endpoint is set, this module
starts a background thread that:
  1. POSTs a signed /api/v1/nodes/join to the main on startup
  2. Periodically POSTs signed /api/v1/nodes/heartbeat

Without this, the worker's heartbeat sender only updates the LOCAL
in-memory store and the main node never sees the worker.

The peer token is resolved in priority order:
  1. The `peer_token` argument (from cluster.token in config)
  2. PEER_TOKEN environment variable
  3. ~/.config/bdaya/hermes-peer-token file
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

logger = logging.getLogger(__name__)


def _resolve_peer_token(explicit: str = "") -> str:
    """Resolve the peer token for signing outbound requests."""
    if explicit:
        return explicit
    import os
    env_token = os.environ.get("PEER_TOKEN", "")
    if env_token:
        return env_token
    # Bdaya fleet convention: shared peer token at ~/.config/bdaya/hermes-peer-token
    token_path = Path.home() / ".config" / "bdaya" / "hermes-peer-token"
    if token_path.is_file():
        return token_path.read_text().strip()
    return ""


def _sign_request(
    token: str, node_id: str, method: str, path: str, body: bytes
) -> Dict[str, str]:
    """Sign a request with HMAC-SHA256, returning auth headers."""
    if not token:
        return {}
    ts = int(time.time())
    body_hash = hashlib.sha256(body).hexdigest()
    message = f"{node_id}:{ts}:{method}:{path}:{body_hash}"
    signature = hmac.new(
        token.encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    return {
        "X-Peer-Node": node_id,
        "X-Peer-Timestamp": str(ts),
        "X-Peer-Signature": signature,
    }


def _signed_post(
    endpoint: str, path: str, data: dict, token: str, node_id: str, timeout: int = 10
) -> Optional[dict]:
    """POST a signed JSON request to the main node."""
    url = f"{endpoint}{path}"
    body = json.dumps(data).encode()
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    headers = _sign_request(token, node_id, "POST", path, body)
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except URLError as e:
        logger.warning("signed POST %s failed: %s", path, e)
        return None
    except Exception as e:
        logger.warning("signed POST %s error: %s", path, e)
        return None


_connector_started = False
_connector_lock = threading.Lock()


def start_worker_connector(
    node_id: str,
    cluster_endpoint: str,
    capabilities: List[str],
    peer_token: str = "",
    heartbeat_interval: float = 30.0,
) -> None:
    """Start the outbound worker connector thread.

    Idempotent: calling twice is a no-op.

    Args:
        node_id: this worker's node ID
        cluster_endpoint: main node URL (e.g. http://127.0.0.1:8787)
        capabilities: list of capability strings
        peer_token: shared secret for signing (resolved from env/file if empty)
        heartbeat_interval: seconds between heartbeat POSTs
    """
    global _connector_started
    with _connector_lock:
        if _connector_started:
            logger.warning("worker connector already running")
            return
        _connector_started = True

    token = _resolve_peer_token(peer_token)
    if not token:
        logger.warning(
            "worker connector: no peer token available — outbound requests "
            "will be unsigned and likely rejected by main's auth middleware"
        )

    # Strip trailing slash from endpoint
    cluster_endpoint = cluster_endpoint.rstrip("/")

    def _loop():
        logger.info(
            "worker connector started: node=%s endpoint=%s interval=%.1fs",
            node_id, cluster_endpoint, heartbeat_interval,
        )

        # Initial join — the main's /join endpoint prepends "node_" to node_name
        join_data = {
            "node_name": node_id,
            "capabilities": capabilities,
            "endpoint": f"http://{node_id}:0",  # worker's own address (informational)
        }
        result = _signed_post(
            cluster_endpoint, "/api/v1/nodes/join", join_data, token, node_id
        )
        # Track the registered node_id (main prepends "node_" to node_name)
        registered_id = node_id
        if result and "node_id" in result:
            registered_id = result["node_id"]
            logger.info("worker connector: join succeeded, registered as %s", registered_id)
        else:
            logger.warning("worker connector: initial join failed — will retry via heartbeat")
            # Fallback: assume the main uses "node_" + our node_id
            registered_id = f"node_{node_id}"

        # Periodic heartbeat — use the registered node_id
        stop_event = threading.Event()
        while not stop_event.is_set():
            stop_event.wait(timeout=heartbeat_interval)
            if stop_event.is_set():
                break
            hb_data = {"node_id": registered_id}
            _signed_post(
                cluster_endpoint, "/api/v1/nodes/heartbeat", hb_data, token, node_id
            )

    thread = threading.Thread(target=_loop, daemon=True, name=f"worker-connector-{node_id}")
    thread.start()
