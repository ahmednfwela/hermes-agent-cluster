"""Worker connector — outbound signed join+heartbeat from worker to main.

When a node runs with role=worker and cluster.endpoint is set, this module
starts a background thread that:
  1. POSTs a signed /api/v1/nodes/join to the main (retried until successful)
  2. Periodically POSTs signed /api/v1/nodes/heartbeat

The heartbeat interval MUST be significantly shorter than the main's
watchdog degraded_after threshold (default 15s) to avoid flapping between
online/degraded. The default 10s interval gives 5s margin below the 15s
degraded threshold and 20s margin below the 30s offline threshold.

Without this, the worker's heartbeat sender only updates the LOCAL
in-memory store and the main node never sees the worker.

The peer token is resolved in priority order:
  1. PEER_TOKEN environment variable (matches app.py's resolution)
  2. ~/.config/bdaya/hermes-peer-token file (Bdaya fleet convention)
  3. The `peer_token` argument (from cluster.token in config)
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
    """Resolve the peer token for signing outbound requests.

    Resolution order matches app.py:92 (env first) to ensure connector
    and plugin signer present the same token to main's per-node map.
    """
    import os
    env_token = os.environ.get("PEER_TOKEN", "")
    if env_token:
        return env_token
    # Bdaya fleet convention: shared peer token at ~/.config/bdaya/hermes-peer-token
    token_path = Path.home() / ".config" / "bdaya" / "hermes-peer-token"
    if token_path.is_file():
        return token_path.read_text().strip()
    if explicit:
        return explicit
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
    heartbeat_interval: float = 10.0,
    max_concurrent: int = 0,
) -> None:
    """Start the outbound worker connector thread.

    Idempotent: calling twice is a no-op.

    Args:
        node_id: this worker's node ID
        cluster_endpoint: main node URL (e.g. http://127.0.0.1:8787)
        capabilities: list of capability strings (from config)
        peer_token: shared secret for signing (resolved from env/file if empty)
        heartbeat_interval: seconds between heartbeat POSTs. MUST be < main's
            watchdog degraded_after (default 15s). Default 10s gives safe margin.
        max_concurrent: maximum simultaneously-assigned tasks this worker can
            run; declared at /join so the main scheduler honours the ceiling
            when assigning tasks (#833). 0 = unlimited.
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
            "worker connector started: node=%s endpoint=%s interval=%.1fs caps=%s",
            node_id, cluster_endpoint, heartbeat_interval, capabilities,
        )

        # Join + heartbeat loop. Join is retried on every cycle until the
        # main accepts it (returns node_id). This handles boot-order races
        # (worker starts before main).
        #
        # NOTE: Post-registration main restarts are NOT handled. The main's
        # ClusterState is in-memory; after a restart, the worker's heartbeat
        # is rejected as "unknown node" but the response is {"status":"ok"}
        # so the connector cannot detect it. The worker remains orphaned
        # until its own process restarts. Fixing this requires the main to
        # return a distinguishable response (e.g. 404 or {"status":"unknown_node"})
        # for unknown-node heartbeats, which is a separate change.
        registered_id = None

        while True:
            # Try join if not yet registered
            if registered_id is None:
                join_data = {
                    "node_name": node_id,
                    "capabilities": capabilities,
                    "endpoint": f"http://{node_id}:0",
                    "max_concurrent": max_concurrent,
                }
                result = _signed_post(
                    cluster_endpoint, "/api/v1/nodes/join", join_data, token, node_id
                )
                if result and "node_id" in result:
                    registered_id = result["node_id"]
                    logger.info(
                        "worker connector: join succeeded, registered as %s",
                        registered_id,
                    )
                else:
                    logger.warning(
                        "worker connector: join failed, will retry in %.1fs",
                        heartbeat_interval,
                    )

            # Send heartbeat if registered
            if registered_id is not None:
                hb_data = {"node_id": registered_id}
                _signed_post(
                    cluster_endpoint, "/api/v1/nodes/heartbeat", hb_data, token, node_id
                )

            time.sleep(heartbeat_interval)

    thread = threading.Thread(target=_loop, daemon=True, name=f"worker-connector-{node_id}")
    thread.start()
