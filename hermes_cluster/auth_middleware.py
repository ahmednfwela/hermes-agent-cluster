"""FastAPI middleware that enforces peer-token auth on federation endpoints.

Applied only to paths under /api/v1/federation/ when peer tokens are configured.
Non-federation paths pass through without auth checks.
"""

from __future__ import annotations

import logging
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .core import peer_auth

logger = logging.getLogger("hermes_cluster.auth_middleware")


class PeerAuthMiddleware(BaseHTTPMiddleware):
    """Verify peer-token HMAC-SHA256 signature on federation endpoints."""

    def __init__(self, app, enabled: bool = False):
        super().__init__(app)
        self.enabled = enabled

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path

        # Only protect federation endpoints
        if not self.enabled or not path.startswith("/api/v1/federation/"):
            return await call_next(request)

        # Read body for signature verification
        body = await request.body()

        # Extract headers
        headers = {
            "X-Peer-Node": request.headers.get("X-Peer-Node", ""),
            "X-Peer-Timestamp": request.headers.get("X-Peer-Timestamp", ""),
            "X-Peer-Signature": request.headers.get("X-Peer-Signature", ""),
        }

        ok, err = peer_auth.verify_request(
            method=request.method,
            path=path,
            body=body,
            headers=headers,
        )

        if not ok:
            logger.warning("Peer auth failed for %s %s: %s", request.method, path, err)
            return JSONResponse(
                status_code=401,
                content={"error": "peer_auth_failed", "detail": err},
            )

        # Reconstruct request with body for downstream handlers
        # (Starlette consumes the body on read, so we need to inject it back)
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = receive
        return await call_next(request)
