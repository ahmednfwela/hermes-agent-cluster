"""bdaya-enforcement hooks -- Python port of the bdaya-defaults enforcement JS hooks.

Four hooks are ported:

1. ``merge_gate`` (pre_tool_call) -- blocks ``approve_and_merge`` / ``mr_merge`` /
   ``glab mr merge`` (and raw-API / external / distribute spellings) unless an
   RV-1 mandate proof is recorded for the run-ledger issue.

2. ``dispatch_dedup_guard`` (pre_tool_call) -- blocks ``delegate_task`` /
   ``Workflow`` / ``Agent`` / dispatch-launches whose text is implementation-shaped
   unless the brief carries a ``DEDUP:`` marker AND an explicit lane-side search
   instruction (``codebase_search`` / ``codebase_symbol`` / ``DEDUP-BY-LANE``).

3. ``proof_or_hedge`` (pre_verify) -- blocks assertive live-system claims with no
   ``[PROOF]`` block or ``[UNVERIFIED]`` hedge.

4. ``mandate_autoarm`` (on_session_start) -- reminder banner at session boot;
   auto-arms a run-ledger pointer when one is discoverable via env/hint.

The predicates are a faithful port of the JS reference in
``plugins/bdaya-defaults/hooks/{mandate-gate,dispatch-dedup-guard,
proof-or-hedge-stop,mandate-autoarm}.js``. Side-effects (subprocess shells into
``bdaya-glab mandate require``, disk state files) are injected via ``deps`` so
the pure logic stays testable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _safe_str(value: Any) -> str:
    return "" if value is None else str(value)


# ---------------------------------------------------------------------------
# 1. merge_gate (pre_tool_call) -- RV-1 mandate gate
# ---------------------------------------------------------------------------

# Gated tool names (typed tools + the shell command surface).
MERGE_TOOL_NAMES = {"approve_and_merge", "mr_merge"}
# Shell-command suffixes whose tool_name ends-with matches the gate.
SHELL_TOOL_NAMES = {"Bash", "PowerShell"}
# External merge commands (deterministic deny -- no GitLab proof path exists).
# Git push push-options that arm a server-side auto-merge.
AUTO_MERGE_OPTION_RE = re.compile(
    r"^merge_request\.(?:auto_merge|merge_when_pipeline_succeeds)\b"
)


@dataclass
class MergeGateVerdict:
    """Outcome of :func:`merge_gate_evaluate`.

    ``deny`` is the human-readable reason; ``verdict`` discriminates a genuine
    missing-proof denial from the UNREADABLE case (every attempt to read GitLab
    failed -- the proof may already be recorded, the reader could not confirm).
    ``allow`` verdicts return ``deny=None``.
    """

    deny: Optional[str] = None
    verdict: Optional[str] = None  # None or "UNREADABLE"
    kind: Optional[str] = None  # "merge" | "distribute" | "external merge"


def _looks_like_shell_merge(cmd: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (kind, detail) if ``cmd`` looks like a gated merge/distribute.

    The port uses regex token inspection with quoted-string stripping --
    mention-in-a-string-is-not-a-command. Sufficient for the Hermes plugin
    surface where tool_input.command is the only input and the commands we gate
    have a small, distinctive vocabulary.
    """
    if not cmd:
        return None, None
    # Strip single- and double-quoted strings so mentions inside commit
    # messages / echo payloads cannot bind a command name.
    stripped = re.sub(r"'[^']*'|\"[^\"]*\"", " ", cmd)

    # External merge: `gh pr merge`
    if re.search(r"\bgh\s+pr\s+merge\b", stripped):
        return "external merge", "gh pr merge (GitHub-side merge)"
    # Git push -o merge_request.* auto-merge
    m = re.search(r"\bgit\s+push\b[^|;]*(-o|--push-option)[= ]([^\s;]+)", stripped)
    if m and AUTO_MERGE_OPTION_RE.match(m.group(2)):
        return (
            "external merge",
            "git push -o merge_request.auto_merge (server-side auto-merge)",
        )
    # `glab mr merge` / `bdaya-glab mr merge`
    if re.search(r"\b(?:bdaya-)?glab\s+mr\s+merge\b", stripped):
        return "merge", None
    # Raw-API merge: `glab api -X PUT .../merge_requests/<iid>/merge`
    if re.search(
        r"\bglab\s+api\b[^|;]*-X\s+PUT\b[^|;]*merge_requests/\d+/merge\b",
        stripped,
        re.IGNORECASE,
    ):
        return "merge", None
    # Testers distribution -- firebase / fastlane / `gh release create|upload|edit`
    if re.search(r"\bfirebase\s+appdistribution:distribute\b", stripped):
        return "distribute", None
    if re.search(r"\bfastlane\s+(?:pilot|deliver)\b", stripped):
        return "distribute", None
    if re.search(r"\bgh\s+release\s+(?:create|upload|edit)\b", stripped):
        return "distribute", None
    return None, None


def _discover_pointer(
    deps: Dict[str, Any],
    cwd: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Discover the armed run pointer.

    Two legs, first match wins (mirrors mandate-autoarm.js):
      1. ``BDAYA_RUN_LEDGER=project#iid`` env var (paired optional
         ``BDAYA_RUN_ID``).
      2. ``.omc/state/bdaya-work/active-run.json`` walking up from ``cwd``.

    Returns ``None`` when no pointer is discoverable -- the gate stands down
    (fail-open by design: an unarmed session has no mandate to enforce).
    """
    env = deps.get("environ", os.environ)
    ledger_ref = _parse_ledger_ref(env.get("BDAYA_RUN_LEDGER", ""))
    if ledger_ref:
        pointer: Dict[str, Any] = {
            "project": ledger_ref["project"],
            "issueIid": ledger_ref["issueIid"],
            "via": "env:BDAYA_RUN_LEDGER",
        }
        run_id = (env.get("BDAYA_RUN_ID") or "").strip()
        if run_id:
            pointer["runId"] = run_id
        return pointer

    cwd_path = Path(cwd or os.getcwd())
    read_json = deps.get("read_json") or _default_read_json
    for directory in [cwd_path, *cwd_path.parents]:
        candidate = directory / ".omc" / "state" / "bdaya-work" / "active-run.json"
        data = read_json(candidate)
        if data and data.get("project") is not None and data.get("issueIid") is not None:
            data.setdefault("via", f"file:{candidate}")
            return data
    return None


def _parse_ledger_ref(raw: str) -> Optional[Dict[str, Any]]:
    s = _safe_str(raw).strip()
    m = re.match(r"^(.+)#(\d+)$", s)
    if not m:
        return None
    return {"project": m.group(1), "issueIid": int(m.group(2))}


def _default_read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _require_proof(deps: Dict[str, Any], query: Dict[str, Any]) -> Dict[str, Any]:
    """Delegate to the configured proof reader. Default: unsatisfied with an
    explicit ``<no reader configured>`` missing-item -- the gate still denies
    (fail-closed on the irreversible action).
    """
    reader = deps.get("require_proof")
    if reader is None:
        return {"allSatisfied": False, "missing": ["<no reader configured>"]}
    try:
        return reader(query)
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc


def _deny_reason(
    kind: str, missing: List[str], pointer: Optional[Dict[str, Any]]
) -> str:
    armed = ""
    if pointer and pointer.get("via"):
        ledger = ""
        if pointer.get("project") is not None and pointer.get("issueIid") is not None:
            ledger = f" (run-ledger {pointer['project']}#{pointer['issueIid']})"
        armed = f"\nArmed by: {pointer['via']}{ledger}."
    return (
        f"[bdaya mandate-gate] DENIED: this {kind} command has no VERIFIED mandate "
        f"proof for: {', '.join(missing)}.\n"
        "GitLab (the run-ledger issue) -- not the transcript -- is the source of "
        "truth. Record the proof first with:  bdaya-glab mandate satisfy --project "
        "<p> --issue <run-iid> --item <ID> --proof <job-trace/upload-url> "
        "[--mr <iid>] [--sha <head>]."
        f"{armed}"
    )


def _external_deny_reason(
    label: str, pointer: Optional[Dict[str, Any]]
) -> str:
    armed = ""
    if pointer and pointer.get("via"):
        armed = f"\nArmed by: {pointer['via']}."
    return (
        f"[bdaya mandate-gate] DENIED: {label} is a merge surface the armed run's "
        "mandate cannot validate -- it has no GitLab MR context for CG-1/RV-1 "
        "proofs, so it is denied under an armed run (issue #57 rows 9/12).\n"
        "For a GitLab MR, merge through the provable path instead:  glab mr merge "
        "<iid> --sha <reviewed-head-sha>  (or the typed mr_merge tool)."
        f"{armed}"
    )


def _unreadable_reason(
    kind: str, attempts: int, error: str, pointer: Optional[Dict[str, Any]]
) -> str:
    armed = ""
    if pointer and pointer.get("via"):
        armed = f"\nArmed by: {pointer['via']}."
    return (
        f"[bdaya mandate-gate] UNREADABLE: could not verify whether a mandate "
        f"proof exists for this {kind} command. GitLab mandate state could not "
        f"be read after {attempts} attempt(s) (last error: {error}).\n"
        "This is NOT a missing-proof denial -- the proof may already be recorded "
        "and fully verified; the reader itself failed. Retry the command."
        f"{armed}"
    )


def _is_typed_merge_tool(tool_name: str) -> bool:
    return tool_name in MERGE_TOOL_NAMES


def _is_opt_out_value(value: Any) -> bool:
    s = _safe_str(value).strip().lower()
    return s in {"1", "true", "yes"}


def _inline_opt_out_matches(cmd: str) -> bool:
    return bool(
        re.search(r"\bBDAYA_ALLOW_UNPROVEN=(?:1|true|yes)\b", cmd, re.IGNORECASE)
    )


def merge_gate_evaluate(
    input: Dict[str, Any], deps: Optional[Dict[str, Any]] = None
) -> Optional[MergeGateVerdict]:
    """Decide whether a pre_tool_call event is a gated merge/distribute.

    Returns ``None`` (allow) when the event is not a gated command OR when no
    run pointer is armed. Returns a :class:`MergeGateVerdict` with a non-empty
    ``deny`` reason on block. Fail-open on any unexpected error for non-typed
    tool surfaces.
    """
    deps = deps or {}
    try:
        tool_name = _safe_str(input.get("tool_name") if input else "")
        if not tool_name:
            return None

        kind: Optional[str] = None
        detail: Optional[str] = None
        cmd: Optional[str] = None

        if _is_typed_merge_tool(tool_name):
            kind = "merge"
        elif tool_name in SHELL_TOOL_NAMES:
            tool_input = input.get("tool_input") or {}
            cmd = _safe_str(tool_input.get("command"))
            kind, detail = _looks_like_shell_merge(cmd)
        else:
            return None

        if kind is None:
            return None

        # Audited escape hatch.
        env = deps.get("environ", os.environ)
        if _is_opt_out_value(env.get("BDAYA_ALLOW_UNPROVEN", "")):
            return None
        if cmd and _inline_opt_out_matches(cmd):
            return None

        pointer = _discover_pointer(deps, cwd=deps.get("cwd"))
        if not pointer:
            return None  # fail open -- no armed run governs this process.

        if kind == "external merge":
            return MergeGateVerdict(
                deny=_external_deny_reason(detail or "external merge", pointer),
                kind=kind,
            )

        query = {
            "project": pointer.get("project"),
            "issueIid": pointer.get("issueIid"),
            "kind": kind,
        }
        attempts = 1
        try:
            res = _require_proof(deps, query)
        except Exception as exc:
            last_error = str(exc)
            return MergeGateVerdict(
                deny=_unreadable_reason(kind, attempts, last_error, pointer),
                verdict="UNREADABLE",
                kind=kind,
            )

        if res and res.get("allSatisfied") is True:
            return None  # proof verified -- allow.

        missing = res.get("missing") if res else None
        if not missing:
            missing = ["<unknown -- GitLab returned no satisfied proof>"]
        return MergeGateVerdict(
            deny=_deny_reason(kind, missing, pointer),
            kind=kind,
        )
    except Exception:
        # Fail open on unexpected errors for non-typed tool surfaces; for typed
        # merge tools the gate keeps its fail-closed posture by returning an
        # UNREADABLE verdict so the irreversible action stays denied.
        if input and _is_typed_merge_tool(_safe_str(input.get("tool_name"))):
            return MergeGateVerdict(
                deny="[bdaya mandate-gate] UNREADABLE: internal error evaluating gate.",
                verdict="UNREADABLE",
                kind="merge",
            )
        return None


# ---------------------------------------------------------------------------
# 2. dispatch_dedup_guard (pre_tool_call)
# ---------------------------------------------------------------------------

# Launch surfaces this guard observes.
DISPATCH_SUFFIXES = ("__dispatch_run", "__fleet_dispatch")

# Subagent types exempt (reviewers / critics verify, they do not duplicate).
EXEMPT_SUBAGENT_RE = re.compile(r"(?:^|:)(bdaya-reviewer|product-critic)$")

# Implementation-shape trigger words (deliberately blunt; see JS header).
TRIGGER_WORDS = ("implement", "MR", "merge", "fix", "land", "mr_create", "approve_and_merge")
TRIGGER_RE = re.compile(r"\b(?:" + "|".join(TRIGGER_WORDS) + r")\b", re.IGNORECASE)

# Anchored self-declaration marker -- the ONLY exemption for implementation-shaped
# text. Optional leading comment token lets a Workflow script carry it while
# staying W1-valid.
REVIEW_ONLY_MARKER_RE = re.compile(
    r"^\s*(?://+|/\*+|#)?\s*(review[- ]only|verify[- ]only|audit[- ]only)\s*:",
    re.IGNORECASE,
)

# DEDUP marker: ``DEDUP:`` followed eventually by an issue/MR ref or ``none found``.
DEDUP_RE = re.compile(
    r"\bDEDUP\s*:[^\n]*(?:#\d+|!\d+|[\w][\w./-]*#\d+|none\s+found)",
    re.IGNORECASE,
)
# Lane-side search instruction: literal token codebase_search / codebase_symbol
# / codebase_symbols, OR a DEDUP-BY-LANE self-declaration.
LANE_SEARCH_RE = re.compile(
    r"\b(?:codebase_search|codebase_symbol|codebase_symbols|DEDUP-BY-LANE)\b"
)


def _has_leading_review_only_marker(text: str) -> bool:
    return bool(REVIEW_ONLY_MARKER_RE.match(text))


def is_implementation_shaped(text: str) -> bool:
    s = _safe_str(text).strip()
    if not s:
        return False
    if _has_leading_review_only_marker(s):
        return False
    return bool(TRIGGER_RE.search(s))


def has_dedup_block(text: str) -> bool:
    return bool(DEDUP_RE.search(_safe_str(text)))


def has_lane_search_instruction(text: str) -> bool:
    return bool(LANE_SEARCH_RE.search(_safe_str(text)))


def _workflow_text(tool_input: Dict[str, Any], deps: Dict[str, Any]) -> Optional[str]:
    if isinstance(tool_input.get("script"), str) and tool_input["script"]:
        return tool_input["script"]
    script_path = tool_input.get("scriptPath")
    if isinstance(script_path, str) and script_path:
        read_file = deps.get("read_file") or _default_read_file
        return read_file(script_path)
    return None


def _default_read_file(path: str) -> Optional[str]:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def dispatch_dedup_evaluate(
    input: Dict[str, Any], deps: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, str]]:
    """Decide whether a launch is implementation-shaped without dedup markers.

    Returns ``{"deny": reason}`` (hard deny), ``{"context": reason}`` (advisory
    mode -- ``BDAYA_DEDUP_GUARD=advisory``), or ``None`` (allow).
    """
    deps = deps or {}
    if not input:
        return None
    # Fail-open on missing session_id -- harness anomaly the agent cannot remedy.
    if not input.get("session_id"):
        return None

    tool_name = _safe_str(input.get("tool_name"))
    tool_input = input.get("tool_input") or {}

    text: Optional[str]
    if tool_name == "Workflow":
        text = _workflow_text(tool_input, deps)
        if text is None:
            return None  # resume-only / name-only launch
    elif tool_name == "Agent":
        sub = tool_input.get("subagent_type")
        if isinstance(sub, str) and EXEMPT_SUBAGENT_RE.search(sub.strip()):
            return None
        parts = [
            tool_input.get("prompt"),
            tool_input.get("description"),
        ]
        text = "\n".join(p for p in parts if isinstance(p, str))
    elif tool_name == "delegate_task" or any(
        tool_name.endswith(suf) for suf in DISPATCH_SUFFIXES
    ):
        prompt = tool_input.get("prompt")
        text = prompt if isinstance(prompt, str) else ""
    else:
        return None

    if not is_implementation_shaped(text):
        return None

    dedup_ok = has_dedup_block(text)
    search_ok = has_lane_search_instruction(text)
    if dedup_ok and search_ok:
        return None

    missing: List[str] = []
    if not dedup_ok:
        missing.append('(a) a "DEDUP:" marker with an issue/MR ref or "none found"')
    if not search_ok:
        missing.append(
            '(b) an explicit lane-side search instruction '
            '(the literal "codebase_search"/"codebase_symbol", '
            'or a "DEDUP-BY-LANE" marker)'
        )
    reason = (
        "DispatchDedupGuard: this launch is missing "
        + " and ".join(missing)
        + ". Dedup is a LANE responsibility, not the lead's "
        "(shared/claude-plugins#645) -- add \"DEDUP: <#issue/!MR ref, or "
        "'none found'>\" and tell the lane to run codebase_search "
        "(or codebase_symbol) itself, or use a \"DEDUP-BY-LANE\" marker. "
        "Reviewer/product-critic Agent launches, and REVIEW-ONLY:/VERIFY-ONLY:/"
        "AUDIT-ONLY: launches, are exempt."
    )

    env = deps.get("environ", os.environ)
    if _safe_str(env.get("BDAYA_DEDUP_GUARD", "")).strip().lower() == "advisory":
        return {"context": reason}
    return {"deny": reason}


# ---------------------------------------------------------------------------
# 3. proof_or_hedge (pre_verify)
# ---------------------------------------------------------------------------

PROOF_COVER_RE = re.compile(r"\[(?:PROOF|UNVERIFIED)\]")

# STRONG -- live-system verdict or remediation command.
STRONG_PATTERNS = [
    re.compile(r"\b(?:deployed|production|prod|live|staging|main branch)\b", re.I),
    re.compile(r"\b(?:is now|has been|already)\s+(?:fixed|merged|shipped|rolled out|enabled)\b", re.I),
    re.compile(r"\b(?:pipeline|CI) (?:passed|is green|succeeded)\b", re.I),
    re.compile(r"\bMR\s+!?\d+\s+(?:merged|approved)\b", re.I),
]

# SOFT -- root-cause / code-correctness.
SOFT_PATTERNS = [
    re.compile(r"\b(?:root cause|root-cause|the bug|the issue) (?:is|was)\b", re.I),
    re.compile(r"\b(?:because|since|as)\s+\w+\s+(?:returns?|calls?|invokes?)\b", re.I),
    re.compile(r"\b(?:this commit|the change|the patch)\s+(?:fixes?|resolves?)\b", re.I),
]


def classify_consequence(text: str) -> str:
    """Return ``'clean'`` | ``'soft'`` | ``'strong'`` for the claim tier."""
    s = _safe_str(text)
    if PROOF_COVER_RE.search(s):
        return "clean"
    for pat in STRONG_PATTERNS:
        if pat.search(s):
            return "strong"
    for pat in SOFT_PATTERNS:
        if pat.search(s):
            return "soft"
    return "clean"


NUDGE = (
    "[bdaya proof-or-hedge] This turn contains an unverified consequential claim. "
    "Attach a proof block or an explicit hedge before the turn is consumed "
    "(constitution #proof-bar):\n"
    "  [PROOF] claim: <one line>\n"
    "    oracle:  <exact command anyone can re-run>\n"
    "    output:  <verbatim key output>\n"
    "    source:  <file:line | URL | /endpoint>\n"
    "  -- or --\n"
    "  [UNVERIFIED] <claim> -- TODO: verify via <how>"
)


BLOCK_REASON = (
    "[bdaya proof-or-hedge] This turn asserts a live-system verdict or remediation "
    "command with no covering [PROOF] block or [UNVERIFIED] hedge.\n"
    "Attach one before the turn is accepted:\n"
    "  Option A -- run the oracle and attach:\n"
    "    [PROOF] claim: <one line>\n"
    "      oracle:  <exact command>\n"
    "      output:  <verbatim key output>\n"
    "      source:  <file:line | URL>\n"
    "  Option B -- acknowledge as unverified:\n"
    "    [UNVERIFIED] <claim> -- TODO: verify via <how>"
)


@dataclass
class ProofOrHedgeState:
    seen_hashes: List[str] = field(default_factory=list)
    total_block_count: int = 0


def _claim_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_state(path: Path) -> ProofOrHedgeState:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ProofOrHedgeState(
            seen_hashes=data.get("seenHashes", []) or [],
            total_block_count=int(data.get("totalBlockCount", 0) or 0),
        )
    except (OSError, ValueError):
        return ProofOrHedgeState()


def _save_state(path: Path, state: ProofOrHedgeState) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "seenHashes": state.seen_hashes,
                    "totalBlockCount": state.total_block_count,
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def proof_or_hedge_evaluate(
    input: Dict[str, Any], deps: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, str]]:
    """Decide whether a pre_verify turn must be blocked for missing proof.

    Returns ``{"block": reason}`` on hard block, ``{"context": reason}`` on a
    nudge (SOFT tier or cap-degrade), or ``None`` when the turn is clean or
    already covered by a ``[PROOF]`` / ``[UNVERIFIED]`` marker.
    """
    deps = deps or {}
    transcript = _safe_str(input.get("transcript") if input else "")
    if not transcript:
        read_transcript = deps.get("read_transcript")
        transcript_path = input.get("transcript_path") if input else None
        if read_transcript and transcript_path:
            try:
                transcript = read_transcript(transcript_path)
            except OSError:
                transcript = ""
        elif transcript_path:
            try:
                transcript = Path(transcript_path).read_text(encoding="utf-8")
            except OSError:
                transcript = ""
    if not transcript:
        return None

    tier = classify_consequence(transcript)
    if tier == "clean":
        return None
    if tier == "soft":
        return {"context": NUDGE}

    # STRONG -- hard block, subject to per-claim dedup + block cap.
    session_id = _safe_str((input or {}).get("session_id")) or "_default"
    state_dir = deps.get("state_dir") or Path.cwd() / ".bdaya-poh-state"
    state_path = Path(state_dir) / f"{session_id}.json"
    load = deps.get("load_state") or _load_state
    save = deps.get("save_state") or _save_state
    state = load(state_path)
    h = _claim_hash(transcript)

    if h in state.seen_hashes:
        return {"context": NUDGE}

    cap = int(deps.get("block_cap", 8))
    if state.total_block_count >= cap - 1:
        state.total_block_count = 0
        save(state_path, state)
        return {"context": NUDGE}

    state.seen_hashes.append(h)
    state.total_block_count += 1
    save(state_path, state)
    return {"block": BLOCK_REASON}


# ---------------------------------------------------------------------------
# 4. mandate_autoarm (on_session_start)
# ---------------------------------------------------------------------------

POINTER_REL = Path(".omc") / "state" / "bdaya-work" / "active-run.json"
HINT_REL = Path(".omc") / "state" / "bdaya-work" / "ledger-hint.json"


def _env_flag_true(value: Any) -> bool:
    return _safe_str(value).strip().lower() in {"1", "true"}


def _default_pointer_exists(cwd: Path) -> bool:
    try:
        data = json.loads((cwd / POINTER_REL).read_text(encoding="utf-8"))
        return bool(data and data.get("project") is not None and data.get("issueIid") is not None)
    except (OSError, ValueError):
        return False


def _default_discover(cwd: Path, env: Dict[str, str]) -> Optional[Dict[str, Any]]:
    ledger = _parse_ledger_ref(env.get("BDAYA_RUN_LEDGER", ""))
    if ledger:
        run_id_raw = env.get("BDAYA_RUN_ID")
        run_id = run_id_raw.strip() if isinstance(run_id_raw, str) and run_id_raw.strip() else None
        found: Dict[str, Any] = {**ledger, "via": "env:BDAYA_RUN_LEDGER"}
        if run_id:
            found["runId"] = run_id
        if _env_flag_true(env.get("BDAYA_AUTOARM_NO_ALLPERISSUE", "")):
            found["allPerIssue"] = False
        return found
    try:
        hint = json.loads((cwd / HINT_REL).read_text(encoding="utf-8"))
        if hint and hint.get("project") is not None and hint.get("issueIid") is not None:
            found = {
                "project": str(hint["project"]),
                "issueIid": int(hint["issueIid"]),
                "via": "file:ledger-hint.json",
            }
            if hint.get("runId") not in (None, ""):
                found["runId"] = str(hint["runId"])
            if hint.get("allPerIssue") is False:
                found["allPerIssue"] = False
            return found
    except (OSError, ValueError):
        pass
    return None


def mandate_autoarm_decide(
    input: Dict[str, Any], deps: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Pure decision: ``{action: 'skip', reason}`` or ``{action: 'arm', pointer, via}``.

    Never overwrites an existing pointer (an armed run owns it). Additive only.
    """
    deps = deps or {}
    cwd = deps.get("cwd") or Path.cwd()
    env = deps.get("environ", os.environ)
    pointer_exists = deps.get("pointer_exists") or (lambda: _default_pointer_exists(cwd))
    discover = deps.get("discover") or (lambda: _default_discover(cwd, env))

    try:
        exists = bool(pointer_exists())
    except Exception:
        exists = False
    if exists:
        return {"action": "skip", "reason": "pointer-exists"}

    try:
        found = discover()
    except Exception:
        found = None
    if not found or found.get("project") is None or found.get("issueIid") is None:
        return {"action": "skip", "reason": "no-discovery"}

    pointer: Dict[str, Any] = {
        "project": str(found["project"]),
        "issueIid": int(found["issueIid"]),
    }
    if found.get("runId") is not None:
        pointer["runId"] = str(found["runId"])
    if found.get("allPerIssue") is not False:
        pointer["allPerIssue"] = True
    session_id = (input or {}).get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        pointer["sessionId"] = session_id
    return {"action": "arm", "pointer": pointer, "via": found.get("via") or "discovery"}


def mandate_autoarm_write(
    pointer: Dict[str, Any], deps: Optional[Dict[str, Any]] = None
) -> None:
    """Write the legacy ``active-run.json`` pointer. Best-effort."""
    deps = deps or {}
    cwd = deps.get("cwd") or Path.cwd()
    target = cwd / POINTER_REL
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone

        payload = dict(pointer)
        payload.setdefault("armedAt", datetime.now(timezone.utc).isoformat())
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Hermes-facing wrappers
# ---------------------------------------------------------------------------


def merge_gate_hook(event: Dict[str, Any], ctx: Optional[Any] = None) -> Dict[str, Any]:
    """Pre-tool-call hook entry point. Returns a Hermes hook result dict."""
    deps = _ctx_to_deps(ctx)
    verdict = merge_gate_evaluate(event, deps)
    if verdict is None:
        return {}
    if verdict.deny:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": verdict.deny,
            }
        }
    return {}


def dispatch_dedup_hook(event: Dict[str, Any], ctx: Optional[Any] = None) -> Dict[str, Any]:
    deps = _ctx_to_deps(ctx)
    res = dispatch_dedup_evaluate(event, deps)
    if not res:
        return {}
    if "deny" in res:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": res["deny"],
            }
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": res.get("context", ""),
        }
    }


def proof_or_hedge_hook(event: Dict[str, Any], ctx: Optional[Any] = None) -> Dict[str, Any]:
    deps = _ctx_to_deps(ctx)
    res = proof_or_hedge_evaluate(event, deps)
    if not res:
        return {}
    if "block" in res:
        return {"decision": "block", "reason": res["block"]}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreVerify",
            "additionalContext": res.get("context", ""),
        }
    }


def mandate_autoarm_hook(event: Dict[str, Any], ctx: Optional[Any] = None) -> Dict[str, Any]:
    deps = _ctx_to_deps(ctx)
    res = mandate_autoarm_decide(event, deps)
    if res.get("action") != "arm":
        return {}
    mandate_autoarm_write(res["pointer"], deps)
    msg = (
        f"[bdaya mandate-autoarm] armed the run-ledger "
        f"{res['pointer']['project']}#{res['pointer']['issueIid']} "
        f"(via {res['via']}) -- mandate enforcement is now ACTIVE for this session."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": msg,
        }
    }


def _ctx_to_deps(ctx: Optional[Any]) -> Dict[str, Any]:
    """Translate a Hermes plugin ctx (when provided) into a deps dict."""
    if ctx is None:
        return {}
    return {"ctx": ctx}
