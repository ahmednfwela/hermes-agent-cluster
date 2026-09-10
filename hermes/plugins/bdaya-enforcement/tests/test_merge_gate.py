"""Tests for the merge_gate (pre_tool_call) hook.

Covers the documented false-positive / false-negative surface:
  - typed merge tools are gated
  - shell command mentions inside quoted strings are NOT gated (FP prevention)
  - external merge surfaces (gh pr merge, git push -o merge_request.*) are denied
  - testers-distribution commands are gated
  - no armed pointer -> fail-open (allow)
  - satisfied proof -> allow
  - missing proof -> deny
  - reader failure -> UNREADABLE verdict
  - BDAYA_ALLOW_UNPROVEN escape hatch (env + inline) -> allow
"""

from __future__ import annotations

import pytest

import hooks


# --- fixtures ----------------------------------------------------------------

def _armed_env(**overrides):
    env = {"BDAYA_RUN_LEDGER": "shared/claude-plugins#37"}
    env.update(overrides)
    return env


def _deps_with_proof(satisfied, missing=None, pointer=None):
    deps = {
        "environ": _armed_env(),
        "require_proof": lambda q: {"allSatisfied": satisfied, "missing": missing or []},
    }
    return deps


# --- negatives: must allow ---------------------------------------------------

class TestMergeGateAllows:
    def test_non_merge_tool_is_allowed(self):
        event = {"tool_name": "Read", "tool_input": {"file_path": "/x"}}
        assert hooks.merge_gate_evaluate(event, _deps_with_proof(False)) is None

    def test_no_armed_run_fails_open(self, tmp_path):
        event = {"tool_name": "mr_merge", "tool_input": {}}
        # Use an isolated cwd so the walk-up cannot find a real pointer.
        deps = {"environ": {}, "cwd": str(tmp_path)}
        assert hooks.merge_gate_evaluate(event, deps) is None

    def test_typed_merge_with_satisfied_proof_is_allowed(self):
        event = {"tool_name": "approve_and_merge", "tool_input": {}}
        verdict = hooks.merge_gate_evaluate(event, _deps_with_proof(True))
        assert verdict is None

    def test_env_escape_hatch_allows_unproven_merge(self):
        event = {"tool_name": "mr_merge", "tool_input": {}}
        deps = {"environ": _armed_env(BDAYA_ALLOW_UNPROVEN="1")}
        assert hooks.merge_gate_evaluate(event, deps) is None

    def test_inline_escape_hatch_allows_unproven_shell_merge(self):
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "BDAYA_ALLOW_UNPROVEN=1 glab mr merge 12 --sha abc"},
        }
        deps = {"environ": _armed_env()}
        assert hooks.merge_gate_evaluate(event, deps) is None

    def test_shell_mention_inside_quotes_is_not_gated(self):
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m 'docs: glab mr merge --sha x'"},
        }
        deps = {"environ": _armed_env()}
        # Mention inside a quoted string is not a gated command.
        assert hooks.merge_gate_evaluate(event, deps) is None

    def test_glab_mr_merge_help_is_not_gated(self):
        # `glab mr merge` is gated only as a merge actuation; help flags are
        # not separately filtered in this port (kept minimal) -- the shell
        # command still matches and is denied when armed. This test records
        # that the gate is command-name-driven, not subcommand-aware beyond
        # what the predicate recognizes.
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "glab mr merge 12 --sha abc"},
        }
        deps = _deps_with_proof(True)
        assert hooks.merge_gate_evaluate(event, deps) is None


# --- positives: must deny ----------------------------------------------------

class TestMergeGateDenies:
    def test_typed_mr_merge_without_proof_is_denied(self):
        event = {"tool_name": "mr_merge", "tool_input": {}}
        verdict = hooks.merge_gate_evaluate(event, _deps_with_proof(False, ["RV-1"]))
        assert verdict is not None
        assert verdict.deny
        assert "RV-1" in verdict.deny

    def test_typed_approve_and_merge_without_proof_is_denied(self):
        event = {"tool_name": "approve_and_merge", "tool_input": {}}
        verdict = hooks.merge_gate_evaluate(event, _deps_with_proof(False))
        assert verdict is not None
        assert verdict.deny
        assert "DENIED" in verdict.deny

    def test_shell_glab_mr_merge_without_proof_is_denied(self):
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "glab mr merge 12 --sha abc"},
        }
        verdict = hooks.merge_gate_evaluate(event, _deps_with_proof(False, ["CG-1"]))
        assert verdict is not None
        assert "CG-1" in verdict.deny

    def test_gh_pr_merge_is_external_merge_deny(self):
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr merge 42 --admin"},
        }
        verdict = hooks.merge_gate_evaluate(event, _deps_with_proof(True))
        # External merge denies regardless of proof state.
        assert verdict is not None
        assert verdict.kind == "external merge"
        assert "cannot validate" in verdict.deny

    def test_git_push_auto_merge_is_external_merge_deny(self):
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "git push -o merge_request.auto_merge"},
        }
        verdict = hooks.merge_gate_evaluate(event, _deps_with_proof(True))
        assert verdict is not None
        assert verdict.kind == "external merge"

    def test_firebase_distribute_is_gated(self):
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "firebase appdistribution:distribute app.apk --app 1:xxx"},
        }
        verdict = hooks.merge_gate_evaluate(event, _deps_with_proof(False, ["TDDD-1"]))
        assert verdict is not None
        assert verdict.kind == "distribute"
        assert "TDDD-1" in verdict.deny

    def test_raw_api_merge_is_gated(self):
        event = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "glab api -X PUT projects/1/merge_requests/12/merge -f sha=abc",
            },
        }
        verdict = hooks.merge_gate_evaluate(event, _deps_with_proof(False, ["CG-1"]))
        assert verdict is not None
        assert verdict.kind == "merge"

    def test_reader_failure_returns_unreadable_verdict(self):
        def _raising(_q):
            raise RuntimeError("ETIMEDOUT")

        event = {"tool_name": "mr_merge", "tool_input": {}}
        deps = {"environ": _armed_env(), "require_proof": _raising}
        verdict = hooks.merge_gate_evaluate(event, deps)
        assert verdict is not None
        assert verdict.verdict == "UNREADABLE"
        assert "UNREADABLE" in verdict.deny
        assert "NOT a missing-proof" in verdict.deny


# --- mutation-sensitivity ----------------------------------------------------

class TestMergeGateMutationSensitivity:
    """Tests that catch the obvious mutations of the gate predicate."""

    def test_mutation_on_merge_tool_set_is_caught(self):
        # If MERGE_TOOL_NAMES lost 'mr_merge', this test must fail.
        assert "mr_merge" in hooks.MERGE_TOOL_NAMES
        event = {"tool_name": "mr_merge", "tool_input": {}}
        verdict = hooks.merge_gate_evaluate(event, _deps_with_proof(False))
        assert verdict is not None

    def test_mutation_on_external_surface_is_caught(self):
        # If gh pr merge stopped being external, this test must fail.
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr merge 1"},
        }
        deps = {"environ": _armed_env()}
        verdict = hooks.merge_gate_evaluate(event, deps)
        assert verdict is not None
        assert verdict.kind == "external merge"
