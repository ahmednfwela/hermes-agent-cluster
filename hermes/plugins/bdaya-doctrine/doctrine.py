"""Doctrine loading, rendering, and checksumming for bdaya-doctrine.

Loads the SOUL.md constitution and reference files, renders the session-start
injection, and computes content checksums for stale-doctrine drift detection.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, Optional

# Plugin root directory
_PLUGIN_DIR = Path(__file__).resolve().parent

# The constitution file
_SOUL_REL = "SOUL.md"

# Reference files that the router can load on signature
_REFERENCES = (
    "core-doctrine.md",
    "merge-policy.md",
    "proof-or-hedge.md",
    "memory-kb.md",
    "watchers.md",
    "mcp-economy.md",
    "git-hygiene.md",
)


def _read_text(rel_path: str) -> Optional[str]:
    """Read a UTF-8 text file relative to the plugin dir; None on any failure."""
    try:
        return (_PLUGIN_DIR / rel_path).read_text(encoding="utf-8")
    except Exception:
        return None


def load_soul() -> Optional[str]:
    """Load the SOUL.md constitution text."""
    return _read_text(_SOUL_REL)


def load_reference(name: str) -> Optional[str]:
    """Load a reference doc by filename (e.g. 'core-doctrine.md').

    Only files in the _REFERENCES allowlist are loadable — prevents path traversal.
    """
    if name not in _REFERENCES:
        return None
    return _read_text(f"references/{name}")


def list_references() -> tuple:
    """Return the tuple of reference filenames the router knows about."""
    return _REFERENCES


def compute_checksums() -> Dict[str, str]:
    """Compute SHA-256 checksums of SOUL.md and every bundled reference.

    Returns a dict mapping relative path → hex digest.  Missing files are
    silently skipped (their absence is itself a drift signal — the caller
    can compare the keyset).
    """
    result: Dict[str, str] = {}
    # SOUL.md
    soul_text = load_soul()
    if soul_text is not None:
        result[_SOUL_REL] = hashlib.sha256(soul_text.encode("utf-8")).hexdigest()
    # references
    for ref in _REFERENCES:
        text = _read_text(f"references/{ref}")
        if text is not None:
            result[f"references/{ref}"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return result


def render_session_injection() -> str:
    """Render the full doctrine injection for on_session_start.

    Combines the SOUL.md constitution text with the router table so the
    session sees both the always-on laws and the deep-load signatures.
    """
    soul = load_soul()
    if soul is None:
        return (
            "bdaya-doctrine: SOUL.md not found — doctrine injection unavailable. "
            "Check plugin installation."
        )
    return soul
