"""Tests for the doctrine_injection (on_session_start) hook.

Covers:
  - hook returns a dict with additional_context
  - injection contains the BCP-14 block
  - injection contains the key constitution sections
  - injection contains the router table
  - injection contains the source-of-trust hierarchy
  - injection contains the proof-or-hedge bar
  - injection contains the complexity ladder
"""

from __future__ import annotations

import pytest

import hooks


class TestDoctrineInjection:
    """on_session_start hook returns the constitution in additional_context."""

    def test_returns_dict(self):
        result = hooks.doctrine_injection_hook({})
        assert isinstance(result, dict)

    def test_has_additional_context(self):
        result = hooks.doctrine_injection_hook({})
        assert "additional_context" in result

    def test_injection_contains_bcp14(self):
        result = hooks.doctrine_injection_hook({})
        text = result["additional_context"]
        # BCP-14 keywords block
        assert "BCP 14" in text
        assert "RFC 2119" in text

    def test_injection_contains_source_of_truth(self):
        result = hooks.doctrine_injection_hook({})
        text = result["additional_context"]
        assert "Source of truth" in text
        assert "running code/tests" in text

    def test_injection_contains_proof_or_hedge(self):
        result = hooks.doctrine_injection_hook({})
        text = result["additional_context"]
        assert "Proof-or-hedge" in text or "proof-or-hedge" in text
        assert "[PROOF]" in text or "oracle" in text

    def test_injection_contains_complexity_ladder(self):
        result = hooks.doctrine_injection_hook({})
        text = result["additional_context"]
        assert "Complexity ladder" in text
        assert "stdlib" in text

    def test_injection_contains_escalate(self):
        result = hooks.doctrine_injection_hook({})
        text = result["additional_context"]
        assert "Escalate" in text
        assert "--no-verify" in text

    def test_injection_contains_validate(self):
        result = hooks.doctrine_injection_hook({})
        text = result["additional_context"]
        assert "Validate" in text

    def test_injection_contains_standing_rules(self):
        result = hooks.doctrine_injection_hook({})
        text = result["additional_context"]
        assert "Standing rules" in text
        assert "Merge only" in text

    def test_injection_contains_router_table(self):
        result = hooks.doctrine_injection_hook({})
        text = result["additional_context"]
        assert "Router" in text
        assert "references/core-doctrine.md" in text
        assert "references/merge-policy.md" in text
        assert "references/proof-or-hedge.md" in text

    def test_injection_contains_bdaya_doctrine_marker(self):
        """The injection must identify itself as bdaya-doctrine."""
        result = hooks.doctrine_injection_hook({})
        text = result["additional_context"]
        assert "bdaya" in text.lower()
        assert "constitution" in text.lower() or "senior engineer" in text.lower()
