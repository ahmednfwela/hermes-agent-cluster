"""bdaya-doctrine — Hermes plugin for senior-engineer doctrine injection.

Registers one hook with the Hermes plugin context:

* ``on_session_start`` doctrine injection (constitution + router)

The pure logic lives in :mod:`hooks` and :mod:`doctrine`; this module is the
thin Hermes-facing registration entry point.
"""

from __future__ import annotations

import logging
from typing import Any

from . import hooks as _hooks

logger = logging.getLogger(__name__)


def register(ctx: Any) -> None:
    """Register bdaya-doctrine hooks with Hermes Agent."""
    registrations = [
        ("on_session_start", _hooks.doctrine_injection_hook, "bdaya doctrine injection"),
    ]
    for event_name, fn, label in registrations:
        try:
            ctx.register_hook(event_name, fn)
        except Exception as exc:  # pragma: no cover - Hermes API surface
            logger.warning("bdaya-doctrine: failed to register %s (%s): %s", label, event_name, exc)
    logger.info("bdaya-doctrine registered %d hooks", len(registrations))


__all__ = ["register"]
