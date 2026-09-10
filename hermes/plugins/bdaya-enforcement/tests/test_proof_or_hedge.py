"""Tests for the proof_or_hedge (pre_verify) hook.

Covers the documented FP/FN surface:
  - clean transcript -> allow
  - [PROOF] coverage -> allow (no block, no nudge)
  - [UNVERIFIED] coverage -> allow
  - SOFT tier -> nudge (context), never hard-block
  - STRONG tier without proof -> hard block (first time)
  - same claim second time -> nudge (per-claim dedup)
  - block-cap valve: one before cap degrades to nudge and resets
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hooks


# --- helpers -----------------------------------------------------------------


def _state_dict(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _assistant_transcript(text: str) -> str:
    """Build a JSON-lines transcript with a single assistant message."""
    msg = {
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        }
    }
    return json.dumps(msg)


# --- negatives: must allow ---------------------------------------------------

class TestProofOrHedgeAllows:
    def test_clean_transcript_is_allowed(self):
        event = {"transcript": _assistant_transcript("Nothing to report today.")}
        assert hooks.proof_or_hedge_evaluate(event, {"state_dir": Path("/tmp/poh-x")}) is None

    def test_proof_block_coverage_allows(self):
        event = {
            "transcript": _assistant_transcript(
                "The pipeline is passing.\n"
                "[PROOF] claim: pipeline passed\n"
                "  oracle:  glab pipeline list\n"
                "  output:  status=passed\n"
                "  source:  gitlab"
            ),
            "session_id": "s1",
        }
        assert hooks.proof_or_hedge_evaluate(event, {"state_dir": Path("/tmp/poh-y")}) is None

    def test_unverified_hedge_coverage_allows(self):
        event = {
            "transcript": _assistant_transcript(
                "The service is deployed. [UNVERIFIED] deployment TODO: check"
            ),
            "session_id": "s1",
        }
        assert hooks.proof_or_hedge_evaluate(event, {"state_dir": Path("/tmp/poh-z")}) is None

    def test_empty_transcript_is_allowed(self):
        event = {"transcript": ""}
        assert hooks.proof_or_hedge_evaluate(event) is None

    def test_non_assertive_mention_is_clean(self):
        """Non-assertive mentions like 'production budget' should be clean (F1 fix)."""
        event = {"transcript": _assistant_transcript("We need to plan the production budget.")}
        assert hooks.proof_or_hedge_evaluate(event, {"state_dir": Path("/tmp/poh-fp")}) is None


# --- nudge (SOFT tier) ------------------------------------------------------

class TestProofOrHedgeSoft:
    def test_soft_claim_returns_context_nudge(self):
        event = {
            "transcript": _assistant_transcript(
                "The bug is because the cache returns stale data."
            )
        }
        res = hooks.proof_or_hedge_evaluate(event, {"state_dir": Path("/tmp/poh-soft")})
        assert res is not None
        assert "context" in res
        assert "block" not in res

    def test_soft_claim_does_not_record_state(self):
        state_dir = Path("/tmp/poh-soft-norec")
        event = {"transcript": _assistant_transcript("The issue was a nil pointer.")}
        hooks.proof_or_hedge_evaluate(event, {"state_dir": state_dir})
        # No per-session state file is written for soft claims.
        assert not any(state_dir.glob("*.json"))


# --- hard block (STRONG tier) ------------------------------------------------

class TestProofOrHedgeBlock:
    def test_strong_claim_blocks_first_time(self, tmp_path):
        event = {
            "transcript": _assistant_transcript(
                "The pipeline is passing and MR !123 is merged."
            ),
            "session_id": "sess-a",
        }
        res = hooks.proof_or_hedge_evaluate(event, {"state_dir": tmp_path})
        assert res is not None
        assert "block" in res
        assert "PROOF" in res["block"]

    def test_same_claim_nudges_on_repeat(self, tmp_path):
        event = {
            "transcript": _assistant_transcript(
                "The pipeline is passing and MR !123 is merged."
            ),
            "session_id": "sess-b",
        }
        first = hooks.proof_or_hedge_evaluate(event, {"state_dir": tmp_path})
        second = hooks.proof_or_hedge_evaluate(event, {"state_dir": tmp_path})
        assert first and "block" in first
        assert second and "context" in second

    def test_block_cap_valve_degrades_to_nudge(self, tmp_path):
        event = {
            "transcript": _assistant_transcript("MR !99 is merged on production."),
            "session_id": "sess-c",
        }
        deps = {"state_dir": tmp_path, "block_cap": 2}
        # First call: total_block_count=0, cap=2, cap-1=1, 0<1 -> hard block.
        first = hooks.proof_or_hedge_evaluate(event, deps)
        # Second call: now total_block_count=1, cap-1=1, 1>=1 -> degrade to nudge,
        # and reset the counter.
        second = hooks.proof_or_hedge_evaluate(event, deps)
        assert first and "block" in first
        assert second and "context" in second

    def test_block_cap_reset_allows_subsequent_blocks(self, tmp_path):
        # Use distinct strong claims so the seen-hash dedup does not intercept.
        e1 = {
            "transcript": _assistant_transcript("MR !99 is merged on production."),
            "session_id": "sess-d",
        }
        deps = {"state_dir": tmp_path, "block_cap": 2}
        first = hooks.proof_or_hedge_evaluate(e1, deps)  # hard block, count -> 1, seen=[h1]
        assert first and "block" in first
        # Different claim: count=1, cap-1=1, 1>=1 -> degrade to nudge AND reset count.
        e2 = {
            "transcript": _assistant_transcript("MR !100 is merged on production."),
            "session_id": "sess-d",
        }
        second = hooks.proof_or_hedge_evaluate(e2, deps)
        assert second and "context" in second
        # After reset: count=0, seen=[h1, h2]. A THIRD new claim should hard-block again.
        e3 = {
            "transcript": _assistant_transcript("MR !101 is merged on production."),
            "session_id": "sess-d",
        }
        third = hooks.proof_or_hedge_evaluate(e3, deps)
        assert third and "block" in third


# --- bug #856: gate tripwire on honest reports ------------------------------

class TestProofOrHedgeTripwire:
    def test_gate_denial_message_does_not_tripwire(self, tmp_path):
        """The gate should not trip on its own denial messages (bug #856)."""
        # Simulate a transcript where the gate denied a merge, and the assistant
        # is reporting that denial. The phrase "pipeline passed" appears in the
        # context of reporting the denial, not as an assertive claim.
        event = {
            "transcript": _assistant_transcript(
                "The mandate-gate denied the merge because RV-1 proof was missing."
            ),
            "session_id": "sess-856",
        }
        # This should be clean (no assertive verb + verdict word pattern)
        res = hooks.proof_or_hedge_evaluate(event, {"state_dir": tmp_path})
        assert res is None


# --- mutation-sensitivity ----------------------------------------------------

class TestProofOrHedgeMutationSensitivity:
    def test_assertive_deployed_is_strong(self):
        """Assertive 'is deployed' should be strong (F1 fix)."""
        assert hooks.classify_consequence("The service is deployed") == "strong"

    def test_non_assertive_production_is_clean(self):
        """Non-assertive 'production budget' should be clean (F1 fix)."""
        assert hooks.classify_consequence("production budget for next quarter") == "clean"

    def test_assertive_server_down_is_strong(self):
        """Assertive 'is down' should be strong (F1 fix)."""
        assert hooks.classify_consequence("The server is down") == "strong"

    def test_root_cause_is_soft(self):
        assert hooks.classify_consequence("The root cause is a nil pointer.") == "soft"

    def test_plain_text_is_clean(self):
        assert hooks.classify_consequence("hello world") == "clean"

    def test_proof_block_makes_strong_clean(self):
        text = "The service is deployed [PROOF] claim: deployed oracle: x output: y source: z"
        assert hooks.classify_consequence(text) == "clean"
