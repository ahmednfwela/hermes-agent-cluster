"""bdaya-doctrine hooks — pure logic, no Hermes imports.

The on_session_start hook injects the senior-engineer constitution into
every new Hermes session, so every agent sees the BCP-14 block, source-of-truth
hierarchy, proof-or-hedge bar, complexity ladder, and standing rules without
needing to load a skill first.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import doctrine

logger = logging.getLogger(__name__)


def doctrine_injection_hook(context: Dict[str, Any]) -> Dict[str, Any]:
    """on_session_start hook — inject the constitution into the session preamble.

    Returns a dict with ``additional_context`` containing the full SOUL.md text.
    Hermes merges this into the system prompt for the session.
    """
    injection = doctrine.render_session_injection()
    if not injection.startswith("bdaya-doctrine: SOUL.md not found"):
        logger.info("bdaya-doctrine: injected constitution (%d chars)", len(injection))
    else:
        logger.warning("bdaya-doctrine: %s", injection)
    return {"additional_context": injection}
