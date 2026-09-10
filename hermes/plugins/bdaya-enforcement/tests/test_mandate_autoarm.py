"""Tests for the mandate_autoarm (on_session_start) hook.

Covers the documented FP/FN surface:
  - existing pointer -> skip (never overwrite)
  - BDAYA_RUN_LEDGER env -> arms via env leg
  - BDAYA_RUN_LEDGER + BDAYA_RUN_ID -> runId carried through
  - BDAYA_AUTOARM_NO_ALLPERISSUE=1 -> allPerIssue omitted
  - ledger-hint.json file -> arms via file leg
  - ledger-hint.json allPerIssue:false -> opt-out respected
  - no discovery -> skip
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hooks


# --- helpers -----------------------------------------------------------------


def _write_hint(cwd: Path, payload: dict) -> None:
    target = cwd / hooks.HINT_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload))


def _write_pointer(cwd: Path, payload: dict) -> None:
    target = cwd / hooks.POINTER_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload))


# --- negatives: must skip ----------------------------------------------------

class TestMandateAutoarmSkips:
    def test_existing_pointer_is_not_overwritten(self, tmp_path):
        _write_pointer(tmp_path, {"project": "x/y", "issueIid": 1})
        deps = {
            "cwd": tmp_path,
            "environ": {"BDAYA_RUN_LEDGER": "other/project#99"},
        }
        res = hooks.mandate_autoarm_decide({"session_id": "s1"}, deps)
        assert res["action"] == "skip"
        assert res["reason"] == "pointer-exists"

    def test_no_discovery_skips(self, tmp_path):
        deps = {"cwd": tmp_path, "environ": {}}
        res = hooks.mandate_autoarm_decide({"session_id": "s1"}, deps)
        assert res["action"] == "skip"
        assert res["reason"] == "no-discovery"

    def test_invalid_ledger_ref_skips(self, tmp_path):
        deps = {"cwd": tmp_path, "environ": {"BDAYA_RUN_LEDGER": "not-a-ref"}}
        res = hooks.mandate_autoarm_decide({"session_id": "s1"}, deps)
        assert res["action"] == "skip"


# --- positives: must arm -----------------------------------------------------

class TestMandateAutoarmArms:
    def test_env_ledger_arms(self, tmp_path):
        deps = {
            "cwd": tmp_path,
            "environ": {"BDAYA_RUN_LEDGER": "shared/claude-plugins#37"},
        }
        res = hooks.mandate_autoarm_decide({"session_id": "s1"}, deps)
        assert res["action"] == "arm"
        assert res["pointer"]["project"] == "shared/claude-plugins"
        assert res["pointer"]["issueIid"] == 37
        assert res["pointer"]["allPerIssue"] is True
        assert res["via"] == "env:BDAYA_RUN_LEDGER"

    def test_env_ledger_carries_run_id(self, tmp_path):
        deps = {
            "cwd": tmp_path,
            "environ": {
                "BDAYA_RUN_LEDGER": "shared/claude-plugins#37",
                "BDAYA_RUN_ID": "run-abc",
            },
        }
        res = hooks.mandate_autoarm_decide({"session_id": "s1"}, deps)
        assert res["pointer"]["runId"] == "run-abc"

    def test_env_allperissue_opt_out(self, tmp_path):
        deps = {
            "cwd": tmp_path,
            "environ": {
                "BDAYA_RUN_LEDGER": "shared/claude-plugins#37",
                "BDAYA_AUTOARM_NO_ALLPERISSUE": "1",
            },
        }
        res = hooks.mandate_autoarm_decide({"session_id": "s1"}, deps)
        assert "allPerIssue" not in res["pointer"]

    def test_hint_file_arms(self, tmp_path):
        _write_hint(tmp_path, {"project": "x/y", "issueIid": 42})
        deps = {"cwd": tmp_path, "environ": {}}
        res = hooks.mandate_autoarm_decide({"session_id": "s1"}, deps)
        assert res["action"] == "arm"
        assert res["pointer"]["project"] == "x/y"
        assert res["pointer"]["issueIid"] == 42
        assert res["via"] == "file:ledger-hint.json"

    def test_hint_file_run_id_carried(self, tmp_path):
        _write_hint(tmp_path, {"project": "x/y", "issueIid": 42, "runId": "run-z"})
        deps = {"cwd": tmp_path, "environ": {}}
        res = hooks.mandate_autoarm_decide({"session_id": "s1"}, deps)
        assert res["pointer"]["runId"] == "run-z"

    def test_hint_file_allperissue_false_opt_out(self, tmp_path):
        _write_hint(tmp_path, {"project": "x/y", "issueIid": 42, "allPerIssue": False})
        deps = {"cwd": tmp_path, "environ": {}}
        res = hooks.mandate_autoarm_decide({"session_id": "s1"}, deps)
        assert "allPerIssue" not in res["pointer"]

    def test_session_id_carried_through(self, tmp_path):
        deps = {
            "cwd": tmp_path,
            "environ": {"BDAYA_RUN_LEDGER": "shared/claude-plugins#37"},
        }
        res = hooks.mandate_autoarm_decide({"session_id": "session-xyz"}, deps)
        assert res["pointer"]["sessionId"] == "session-xyz"


# --- write side --------------------------------------------------------------

class TestMandateAutoarmWrite:
    def test_write_creates_pointer_file(self, tmp_path):
        pointer = {"project": "x/y", "issueIid": 12}
        hooks.mandate_autoarm_write(pointer, {"cwd": tmp_path})
        target = tmp_path / hooks.POINTER_REL
        assert target.exists()
        data = json.loads(target.read_text())
        assert data["project"] == "x/y"
        assert data["issueIid"] == 12
        assert "armedAt" in data


# --- mutation-sensitivity ----------------------------------------------------

class TestMandateAutoarmMutationSensitivity:
    def test_pointer_rel_path_is_stable(self):
        assert str(hooks.POINTER_REL) == str(Path(".omc/state/bdaya-work/active-run.json"))

    def test_hint_rel_path_is_stable(self):
        assert str(hooks.HINT_REL) == str(Path(".omc/state/bdaya-work/ledger-hint.json"))

    def test_parse_ledger_ref_roundtrip(self):
        r = hooks._parse_ledger_ref("shared/claude-plugins#37")
        assert r == {"project": "shared/claude-plugins", "issueIid": 37}
