"""Tests for the dispatch_dedup_guard (pre_tool_call) hook.

Covers the documented FP/FN surface:
  - implementation-shaped Agent launches without DEDUP + lane-search are denied
  - REVIEW-ONLY:/VERIFY-ONLY:/AUDIT-ONLY: leading markers exempt
  - bdaya-reviewer and product-critic subagent types are exempt
  - DEDUP + codebase_search together -> allow
  - DEDUP-BY-LANE marker satisfies the lane-search leg
  - non-implementation-shaped briefs are allowed
  - Workflow tool resume (no script) is allowed
  - BDAYA_DEDUP_GUARD=advisory degrades to a nudge
  - missing session_id -> fail-open
  - non-launch tool is allowed
"""

from __future__ import annotations

import pytest

import hooks


def _base(**overrides):
    env = {}
    env.update(overrides)
    return env


# --- negatives: must allow ---------------------------------------------------

class TestDispatchDedupAllows:
    def test_non_launch_tool_is_allowed(self):
        event = {
            "tool_name": "Read",
            "tool_input": {"file_path": "/x"},
            "session_id": "abc",
        }
        assert hooks.dispatch_dedup_evaluate(event) is None

    def test_non_implementation_shaped_is_allowed(self):
        event = {
            "tool_name": "Agent",
            "tool_input": {"prompt": "Gather metrics about CPU usage.", "subagent_type": "Explore"},
            "session_id": "abc",
        }
        assert hooks.dispatch_dedup_evaluate(event) is None

    def test_review_only_marker_exempts(self):
        event = {
            "tool_name": "Agent",
            "tool_input": {
                "prompt": "REVIEW-ONLY: review MR !123 and implement the feedback",
                "subagent_type": "bdaya-reviewer",
            },
            "session_id": "abc",
        }
        assert hooks.dispatch_dedup_evaluate(event) is None

    def test_verify_only_marker_exempts(self):
        event = {
            "tool_name": "Agent",
            "tool_input": {
                "prompt": "VERIFY-ONLY: verify MR !123, then fix anything you find",
                "subagent_type": "general-purpose",
            },
            "session_id": "abc",
        }
        assert hooks.dispatch_dedup_evaluate(event) is None

    def test_audit_only_marker_exempts(self):
        event = {
            "tool_name": "Agent",
            "tool_input": {
                "prompt": "AUDIT-ONLY: audit the diff and implement follow-ups",
                "subagent_type": "general-purpose",
            },
            "session_id": "abc",
        }
        assert hooks.dispatch_dedup_evaluate(event) is None

    def test_review_only_in_comment_prefix_exempts(self):
        event = {
            "tool_name": "Workflow",
            "tool_input": {"script": "// REVIEW-ONLY: review the diff\nexport const meta = {};"},
            "session_id": "abc",
        }
        assert hooks.dispatch_dedup_evaluate(event) is None

    def test_bdaya_reviewer_subagent_is_exempt(self):
        event = {
            "tool_name": "Agent",
            "tool_input": {
                "prompt": "Implement a new MR for this issue",
                "subagent_type": "bdaya-reviewer",
            },
            "session_id": "abc",
        }
        assert hooks.dispatch_dedup_evaluate(event) is None

    def test_product_critic_subagent_is_exempt(self):
        event = {
            "tool_name": "Agent",
            "tool_input": {
                "prompt": "Implement a verification of the fix",
                "subagent_type": "plugin:bdaya:product-critic",
            },
            "session_id": "abc",
        }
        assert hooks.dispatch_dedup_evaluate(event) is None

    def test_full_dedup_marker_allows(self):
        event = {
            "tool_name": "Agent",
            "tool_input": {
                "prompt": (
                    "Implement the fix for issue #123.\n"
                    "DEDUP: #123 -- lane should run codebase_search on arrival."
                ),
                "subagent_type": "general-purpose",
            },
            "session_id": "abc",
        }
        assert hooks.dispatch_dedup_evaluate(event) is None

    def test_dedup_by_lane_marker_satisfies_search_leg(self):
        event = {
            "tool_name": "Agent",
            "tool_input": {
                "prompt": "Implement MR. DEDUP: none found. DEDUP-BY-LANE",
                "subagent_type": "general-purpose",
            },
            "session_id": "abc",
        }
        assert hooks.dispatch_dedup_evaluate(event) is None

    def test_workflow_resume_without_script_is_allowed(self):
        event = {
            "tool_name": "Workflow",
            "tool_input": {"name": "resume-existing"},
            "session_id": "abc",
        }
        assert hooks.dispatch_dedup_evaluate(event) is None

    def test_missing_session_id_fails_open(self):
        event = {
            "tool_name": "Agent",
            "tool_input": {"prompt": "Implement MR for #123", "subagent_type": "general-purpose"},
        }
        assert hooks.dispatch_dedup_evaluate(event) is None


# --- positives: must deny ----------------------------------------------------

class TestDispatchDedupDenies:
    def test_implementation_shaped_without_dedup_is_denied(self):
        event = {
            "tool_name": "Agent",
            "tool_input": {
                "prompt": "Implement the fix for issue #123 and open an MR.",
                "subagent_type": "general-purpose",
            },
            "session_id": "abc",
        }
        res = hooks.dispatch_dedup_evaluate(event)
        assert res is not None
        assert "deny" in res
        assert "DEDUP" in res["deny"]

    def test_implementation_shaped_with_dedup_only_is_denied(self):
        event = {
            "tool_name": "Agent",
            "tool_input": {
                "prompt": "Implement fix. DEDUP: #123",  # no lane search instruction
                "subagent_type": "general-purpose",
            },
            "session_id": "abc",
        }
        res = hooks.dispatch_dedup_evaluate(event)
        assert res is not None
        assert "codebase_search" in res["deny"]

    def test_implementation_shaped_with_search_only_is_denied(self):
        event = {
            "tool_name": "Agent",
            "tool_input": {
                "prompt": "Run codebase_search for MR #123, then implement",  # no DEDUP marker
                "subagent_type": "general-purpose",
            },
            "session_id": "abc",
        }
        res = hooks.dispatch_dedup_evaluate(event)
        assert res is not None
        assert "DEDUP" in res["deny"]

    def test_delegate_task_launch_is_gated(self):
        event = {
            "tool_name": "delegate_task",
            "tool_input": {"prompt": "Implement the MR for issue #42"},
            "session_id": "abc",
        }
        res = hooks.dispatch_dedup_evaluate(event)
        assert res is not None
        assert "deny" in res

    def test_dispatch_run_suffix_is_gated(self):
        event = {
            "tool_name": "mcp__bdaya__dispatch_run",
            "tool_input": {"prompt": "Fix MR !123 feedback"},
            "session_id": "abc",
        }
        res = hooks.dispatch_dedup_evaluate(event)
        assert res is not None
        assert "deny" in res

    def test_fleet_dispatch_suffix_is_gated(self):
        event = {
            "tool_name": "mcp__bdaya__fleet_dispatch",
            "tool_input": {"prompt": "Implement the fix"},
            "session_id": "abc",
        }
        res = hooks.dispatch_dedup_evaluate(event)
        assert res is not None
        assert "deny" in res

    def test_workflow_script_without_dedup_is_denied(self):
        event = {
            "tool_name": "Workflow",
            "tool_input": {"script": "export const meta = {}; implement MR for #123"},
            "session_id": "abc",
        }
        res = hooks.dispatch_dedup_evaluate(event)
        assert res is not None
        assert "deny" in res


# --- advisory mode -----------------------------------------------------------

class TestDispatchDedupAdvisory:
    def test_advisory_mode_returns_context_not_deny(self):
        event = {
            "tool_name": "Agent",
            "tool_input": {
                "prompt": "Implement the fix",
                "subagent_type": "general-purpose",
            },
            "session_id": "abc",
        }
        deps = {"environ": {"BDAYA_DEDUP_GUARD": "advisory"}}
        res = hooks.dispatch_dedup_evaluate(event, deps)
        assert res is not None
        assert "context" in res
        assert "deny" not in res


# --- mutation-sensitivity ----------------------------------------------------

class TestDispatchDedupMutationSensitivity:
    def test_trigger_words_include_fix(self):
        assert "fix" in hooks.TRIGGER_WORDS

    def test_trigger_words_include_mr(self):
        assert "MR" in hooks.TRIGGER_WORDS

    def test_exempt_subagent_re_matches_reviewer(self):
        assert hooks.EXEMPT_SUBAGENT_RE.search("bdaya-reviewer")
        assert hooks.EXEMPT_SUBAGENT_RE.search("plugin:bdaya:bdaya-reviewer")

    def test_exempt_subagent_re_does_not_match_worker(self):
        assert not hooks.EXEMPT_SUBAGENT_RE.search("general-purpose")
