"""bdaya-enforcement -- Hermes plugin port of the bdaya-defaults enforcement hooks.

Registers four hooks with the Hermes plugin context:

* ``pre_tool_call`` merge gate (RV-1 mandate)
* ``pre_tool_call`` dispatch-dedup guard
* ``pre_verify`` proof-or-hedge
* ``on_session_start`` mandate-autoarm banner

The pure predicate logic lives in :mod:`hooks`; this module is the thin
Hermas-facing registration entry point.
"""

from __future__ import annotations

import logging
from typing import Any

from . import hooks as _hooks

logger = logging.getLogger(__name__)


def register(ctx: Any) -> None:
    """Register bdaya-enforcement hooks with Hermes Agent."""
    registrations = [
        ("pre_tool_call", _hooks.merge_gate_hook, "bdaya mandate-gate (RV-1)"),
        ("pre_tool_call", _hooks.dispatch_dedup_hook, "bdaya dispatch-dedup guard"),
        ("pre_verify", _hooks.proof_or_hedge_hook, "bdaya proof-or-hedge"),
        ("on_session_start", _hooks.mandate_autoarm_hook, "bdaya mandate-autoarm banner"),
    ]
    for event_name, fn, label in registrations:
        try:
            ctx.register_hook(event_name, fn)
        except Exception as exc:  # pragma: no cover - Hermes API surface
            logger.warning("bdaya-enforcement: failed to register %s (%s): %s", label, event_name, exc)
    logger.info("bdaya-enforcement registered %d hooks", len(registrations))


__all__ = ["register"]
