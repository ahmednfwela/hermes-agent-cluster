"""Cross-node HTTP client with peer-token authentication.

Provides authenticated HTTP calls to peer nodes for:
- Task sync (push/pull)
- Heartbeat
- Node coordination

All outgoing requests are signed with the local node's peer token.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from . import peer_auth

logger = logging.getLogger("hermes_cluster.peer_client")


class PeerClient:
    """HTTP client for cross-node communication with peer-token auth."""

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        json: Optional[Dict[str, Any]] = None,
        content: Optional[bytes] = None,
    ) -> httpx.Response:
        """Make an authenticated request to a peer node.

        Automatically signs the request with peer auth headers.
        """
        # Prepare body
        if json is not None:
            import json as json_module
            body = json_module.dumps(json).encode()
            headers = {"Content-Type": "application/json"}
        elif content is not None:
            body = content
            headers = {}
        else:
            body = b""
            headers = {}

        # Sign the request (D2: wire sign_request into outgoing calls)
        if peer_auth._local_token and peer_auth._local_node_id:
            # Extract path from URL for signing
            from urllib.parse import urlparse
            parsed = urlparse(url)
            path = parsed.path

            auth_headers = peer_auth.sign_request(method, path, body)
            headers.update(auth_headers)

        client = await self._get_client()
        response = await client.request(method, url, content=body, headers=headers)
        return response

    async def post(self, url: str, json: Optional[Dict[str, Any]] = None) -> httpx.Response:
        """POST with peer auth."""
        return await self.request("POST", url, json=json)

    async def get(self, url: str) -> httpx.Response:
        """GET with peer auth."""
        return await self.request("GET", url)


# Module-level singleton for convenience
_default_client: Optional[PeerClient] = None


def get_peer_client() -> PeerClient:
    """Get the default peer client instance."""
    global _default_client
    if _default_client is None:
        _default_client = PeerClient()
    return _default_client


async def close_peer_client():
    """Close the default peer client."""
    global _default_client
    if _default_client:
        await _default_client.close()
        _default_client = None
