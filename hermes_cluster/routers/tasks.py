"""Task management endpoints — /api/v1/tasks"""

import logging
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

from ..models import (
    DEFAULT_PRIORITY,
    SubmitTaskRequest,
    CompleteTaskRequest,
    FailTaskRequest,
    CancelTaskRequest,
    SetDependenciesRequest,
    ClaimTaskRequest,
    ReleaseTaskRequest,
    Task,
    TaskStatus,
)
from ..state import ClusterState

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

_state: ClusterState = None
_lease_manager = None

def init(state: ClusterState, lease_manager=None):
    global _state, _lease_manager
    _state = state
    _lease_manager = lease_manager


def _generate_task_id() -> str:
    import secrets
    return "task_" + secrets.token_hex(8)


def _lane_target(lane_key: str):
    """The PR/MR or issue number a lane_key names, if it names one (#872).

    `infra-github!274-rev-b` -> "274".  `claude-plugins!912-rev2` -> "912".
    A branch-shaped key like `claude-plugins#feat/869-seat-by-paste` names no
    number (the segment after # is not digits) and returns None -- those lanes
    carry no target to disagree with.
    """
    m = re.search(r"[!#](\d+)", lane_key or "")
    return m.group(1) if m else None


def _brief_names_target(title: str, target: str) -> bool:
    """True if the brief mentions the target as a NUMBER, not a substring.

    Bounded so "274" is not satisfied by "1274" or "2740" -- an unbounded match
    would let a brief about a different PR pass while appearing to guard.
    """
    return re.search(r"(?<!\d)" + re.escape(target) + r"(?!\d)", title or "") is not None


# A TARGET REF is a number written in a shape that means "this PR/MR/issue":
#   `#274`, `!274`, `pr#275`, `MR 275`, `gh pr comment 273`, `work item !912`.
# Deliberately NOT every bare number in the brief (line counts, budgets, head
# SHAs) -- only references with target-referrer tokens, so the guard stays
# usable for real briefs while still catching a mis-copied target.
_TARGET_REF = re.compile(
    r"""
      [!#]\s*(\d+)                                    # gitlab-style: !274 / #872
    | \b(?:pr|mr|merge\s+request|issue|work\s+item)s?\b   # a referrer token
      (?:\s*[!#])?\s*(?:[A-Za-z_]{1,12}\s+)?          # optional !/# + one action word
      (\d+)                                           # the number it aims at
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _foreign_target_refs(title: str, target: str) -> list:
    """Target-ref numbers named in the brief that are NOT the lane's target.

    #872's second instance: lane `infra-github!275-rev-c` got a brief that
    named pr#275 (so the presence check passed) but instructed the lane to
    `gh pr comment 273` -- the wrong PR. The lane overrode its own brief and
    guessed right; a lane guessing at its target is luck, not a control.
    Any foreign target ref means the brief and the lane disagree about WHICH
    work item this delivery is about, which is the whole defect.
    """
    found = set()
    for m in _TARGET_REF.finditer(title or ""):
        num = m.group(1) or m.group(2)
        if num and num != target:
            found.add(num)
    return sorted(found)


# An ACTION reference: a posting/reviewing verb with a bounded target number
# attached -- "gh pr comment 273", "review PR#274", "post the verdict to 275",
# "merge 273". Verb-only mentions ("NEVER approve or merge") carry no number
# and match nothing. Bare cross-references WITHOUT a verb (`Refs #872`) are
# context, not the job instruction, so they are deliberately NOT captured --
# flagging them would false-reject ordinary reviewer briefs.
_ACTION_REF_RE = re.compile(
    r"(?i)\b(?:"
    r"(?:gh\s+)?pr\s+comment"
    r"|comment\s+on"
    r"|post\s+(?:the\s+\w+\s+)?(?:to|on|in)"
    r"|review(?:ed)?(?:\s+(?:the\s+)?(?:pr|mr|issue))?"
    r"|verdict\s+(?:on|for)"
    r"|(?:merge|close)(?:d)?\s+(?:the\s+)?(?:pr|mr|issue)?"
    r")\s*#?\s*(\d+)(?!\d)"
)


def _brief_action_targets(title: str) -> set:
    """Every bounded number the brief attaches to a posting/reviewing verb.

    Instance 2 of #872: a brief for the PR#275 lane told the lane to
    `gh pr comment 273`. The mention-check alone accepted it -- the title
    DID mention 275 -- and only the lane's own override saved the verdict.
    A brief whose ACTION names a different target than the lane does is the
    same defect: the lane is being asked to do the wrong job.
    """
    return set(_ACTION_REF_RE.findall(title or ""))


@router.post("")
async def submit_task(req: SubmitTaskRequest):
    # #872: a task's `title` IS its brief -- the schema has no description
    # column. On 2026-09-12 the authoring path wrote one task's brief verbatim
    # into another task's title: the reviewer lane for PR#274 received an
    # IMPLEMENTATION brief naming no PR at all. The lane did exactly as asked
    # and posted nothing; the lead read `completed` with no verdict, concluded
    # the result was lost, and paid for a re-review plus a 13-agent diagnosis.
    # There was never a lost verdict -- only a brief that did not match its lane.
    target = _lane_target(req.lane_key)
    if target and not _brief_names_target(req.title, target):
        raise HTTPException(
            status_code=422,
            detail=(
                f"brief/target disagreement (#872): lane_key {req.lane_key!r} "
                f"names target {target}, but the title -- which IS the brief -- "
                f"never mentions it. The lane would run the wrong job and report "
                f"completed. Fix the brief, or the lane_key."
            ),
        )
    # Instance 2 of #872: the brief MENTIONED its target (275) while
    # instructing `gh pr comment 273`. A mention-only check accepted it; only
    # the lane overriding its own brief saved that verdict. If the brief's
    # ACTION names a different number than the lane does, reject too -- but
    # only when the target is absent from the action set entirely, so a brief
    # that reviews 275 and cross-references 273 stays legal.
    if target:
        actions = _brief_action_targets(req.title)
        if actions and target not in actions:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"brief/action disagreement (#872 instance 2): lane_key "
                    f"{req.lane_key!r} names target {target}, but the title's "
                    f"posting/reviewing instruction(s) name only "
                    f"{sorted(actions)}. The lane would post its verdict to the "
                    f"wrong PR -- or silently override its own brief and get "
                    f"lucky. Fix the brief."
                ),
            )

    task_id = _generate_task_id()
    # Default only when the caller said nothing (None). 0 is a legal band —
    # the top one — and must survive to the store untouched (#866). Range
    # 0..5 is validated by SubmitTaskRequest, so out-of-band is already 422.
    priority = DEFAULT_PRIORITY if req.priority is None else req.priority
    task = _state.create_task(
        task_id,
        req.title,
        req.requires,
        priority,
        lane_key=req.lane_key,
        role=req.role,
    )
    # Promote pending → ready (tasks with no deps go to ready immediately)
    # But do NOT auto-assign to nodes — use /schedule/trigger for that
    _state.trigger_pending_tasks()
    return task


@router.get("")
async def list_tasks():
    return _state.get_all_tasks()


@router.get("/{task_id}")
async def get_task(task_id: str):
    """Read ONE task, including its deliverable (#874).

    Until now the only read path was the full listing -- so fetching a single
    lane's result meant pulling every task in the cluster and filtering client
    side, and the lead had no per-task read at all. Retrievability is the whole
    point of #874; a result you cannot address is barely stored.
    """
    task = _state.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@router.post("/{task_id}/complete")
async def complete_task(task_id: str, req: Optional[CompleteTaskRequest] = None):
    """Close a task, optionally carrying its deliverable (#874).

    `req` is optional so callers that post no body keep working unchanged. When
    a result IS supplied it is stored on the task row, which is what makes a
    lane's output readable from a node other than the one that produced it.
    """
    task = _state.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")

    # B2 fix: terminal states (completed/failed/cancelled) → 409
    # cancel_requested is allowed through (worker ack path)
    if task.status in (TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled):
        raise HTTPException(
            status_code=409,
            detail=f"task is already terminal (status={task.status.value})",
        )

    # Revoke lease
    if _lease_manager:
        lease = _lease_manager.get_by_task(task_id)
        if lease:
            _lease_manager.revoke(lease.id)

    # If task was cancel_requested, worker ack closes it to cancelled
    if task.status == TaskStatus.cancel_requested:
        _state.set_task_status(task_id, TaskStatus.cancelled, fail_reason="cancelled")
        return {"status": "cancelled"}

    # Record the deliverable BEFORE the status flip, so a reader that sees
    # `completed` never sees it without the result that completion refers to.
    stored = False
    if req is not None and req.result is not None:
        stored = _state.set_task_result(task_id, req.result)

    _state.set_task_status(task_id, TaskStatus.completed)
    # Auto-transition downstream tasks
    _trigger_downstream(task_id)
    return {"status": "completed", "result_stored": stored}


@router.post("/{task_id}/fail")
async def fail_task(task_id: str, req: FailTaskRequest = None):
    task = _state.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    reason = req.reason if req else "failed"

    # B2 fix: terminal states (completed/failed/cancelled) → 409
    # cancel_requested is allowed through (worker ack path)
    if task.status in (TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled):
        raise HTTPException(
            status_code=409,
            detail=f"task is already terminal (status={task.status.value})",
        )

    # If task was cancel_requested, worker ack closes it to cancelled
    if task.status == TaskStatus.cancel_requested:
        _state.set_task_status(task_id, TaskStatus.cancelled, fail_reason=reason)
        return {"status": "cancelled", "blocked": []}

    # #870: a worker reporting a NON-DELIVERABLE result body (provider error
    # / echoed brief) asks for a re-queue instead of consumption. Main owns
    # the retry cap — requeue_task bumps `attempts` and returns the task to
    # ready atomically, refusing at/over the cap; on refusal (or a terminal
    # task) we fall through to the consuming failure below, so the cap is
    # enforced in exactly one place: the store's guarded UPDATE.
    if req and getattr(req, "requeue", False):
        requeued = False
        try:
            requeued = _state.requeue_task(task_id, reason=reason)
        except Exception:
            logger.exception("requeue_task failed for %s — consuming instead",
                             task_id)
        if requeued:
            # Mirror the recovery rescheduler: the queue gets an immediate
            # chance to re-place the task without waiting for an external
            # /schedule/trigger (the whole point of re-queueing is that the
            # work continues). Best-effort: the task stays `ready` either
            # way and a later trigger still picks it up.
            try:
                _state.schedule_pending()
            except Exception:
                logger.exception("schedule_pending after requeue of %s failed",
                                 task_id)
            return {"status": "requeued", "requeued": True, "reason": reason}

    # N2 fix: revoke lease on /fail (same as /complete does)
    if _lease_manager:
        lease = _lease_manager.get_by_task(task_id)
        if lease:
            _lease_manager.revoke(lease.id)

    _state.set_task_status(task_id, TaskStatus.failed, fail_reason=reason)
    # S4 fix: transitive cascade-cancel ALL non-terminal dependents
    blocked = _cascade_cancel_dependents(task_id, f"parent {task_id} failed")
    return {"status": "failed", "blocked": blocked}


def _cascade_cancel_dependents(task_id: str, reason: str) -> list:
    """S4 fix: transitively cancel ALL non-terminal dependents of task_id.

    Walks the dependent tree depth-first. Cancels any dependent that is not
    already terminal (completed/failed/cancelled/cancel_requested). Running
    dependents get their leases revoked and are set to cancel_requested.
    Returns list of all dependent task_ids found (for API compatibility).
    """
    all_dependents = _state.get_trigger_chain(task_id)  # transitive, depth-first
    for dep_id in all_dependents:
        dep_task = _state.get_task(dep_id)
        if not dep_task:
            continue
        terminal = {TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled, TaskStatus.cancel_requested}
        if dep_task.status in terminal:
            continue
        # R3-1 fix: branch on lease existence (like cancel_task S2), not status.
        # Running + unleased (scheduler-assigned) → cancelled immediately, not cancel_requested zombie.
        lease = _lease_manager.get_by_task(dep_id) if _lease_manager else None
        if lease is not None:
            _lease_manager.revoke(lease.id)
            _state.set_task_status(dep_id, TaskStatus.cancel_requested, fail_reason=reason)
        else:
            _state.set_task_status(dep_id, TaskStatus.cancelled, fail_reason=reason)
    return all_dependents


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, req: CancelTaskRequest = None):
    """Cancel a task — two-phase for running tasks, immediate for unclaimed.

    Branching is on lease existence (S2), not status:
    - No lease → cancelled immediately (regardless of status)
    - Has lease → lease revoked, → cancel_requested; worker's next
      /complete or /fail closes to cancelled
    - Terminal (completed/failed/cancelled/cancel_requested) → 409
    After a successful cancel, transitively cancels all non-terminal dependents (S4).
    """
    task = _state.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")

    reason = req.reason if req else "cancelled"

    # Terminal states → 409 (ERR_TASK_NOT_CANCELABLE)
    if task.status in (
        TaskStatus.completed,
        TaskStatus.failed,
        TaskStatus.cancelled,
        TaskStatus.cancel_requested,
    ):
        raise HTTPException(
            status_code=409,
            detail=f"task is not cancelable (status={task.status.value})",
        )

    # S2 fix: branch on lease existence, not status (per #799 note 131686 item 8)
    # No lease → cancelled immediately; has lease → revoke, → cancel_requested
    lease = _lease_manager.get_by_task(task_id) if _lease_manager else None
    if lease is None:
        _state.set_task_status(task_id, TaskStatus.cancelled, fail_reason=reason)
        # S4 fix: cascade-cancel dependents
        _cascade_cancel_dependents(task_id, f"parent {task_id} cancelled")
        return {"status": "cancelled", "phase": "immediate"}

    # Has lease → revoke it, → cancel_requested
    _lease_manager.revoke(lease.id)
    _state.set_task_status(task_id, TaskStatus.cancel_requested, fail_reason=reason)
    # S4 fix: cascade-cancel dependents
    _cascade_cancel_dependents(task_id, f"parent {task_id} cancelled")
    return {"status": "cancel_requested", "phase": "pending_ack"}


@router.post("/{task_id}/unblock")
async def unblock_task(task_id: str):
    if not _state.unblock_task(task_id):
        raise HTTPException(status_code=400, detail="task not in blocked state")
    return {"status": "unblocked"}


@router.post("/{task_id}/advance")
async def manual_advance(task_id: str):
    task = _state.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    # B1 fix: reject advance for terminal/cancel states
    if task.status in (
        TaskStatus.completed,
        TaskStatus.failed,
        TaskStatus.cancelled,
        TaskStatus.cancel_requested,
    ):
        raise HTTPException(
            status_code=409,
            detail=f"task is terminal (status={task.status.value}), cannot advance",
        )
    # Try to resolve dependencies
    if task.depends_on:
        all_done = all(
            (dep := _state.get_task(dep_id)) is not None
            and dep.status == TaskStatus.completed
            for dep_id in task.depends_on
        )
        if not all_done:
            raise HTTPException(status_code=400, detail="dependencies not met")
    _state.set_task_status(task_id, TaskStatus.ready)
    _state.schedule_pending()
    return {"status": "advanced"}


@router.post("/{task_id}/dependencies")
async def set_dependencies(task_id: str, req: SetDependenciesRequest):
    task = _state.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    _state.set_dependencies(task_id, req.depends_on)
    return _state.get_task(task_id)


@router.get("/{task_id}/dependents")
async def get_dependents(task_id: str):
    dependents = _state.get_dependents(task_id)
    return {"task_id": task_id, "dependents": dependents, "count": len(dependents)}


@router.get("/{task_id}/trigger-chain")
async def get_trigger_chain(task_id: str):
    task = _state.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    chain = _state.get_trigger_chain(task_id)
    return {"task_id": task_id, "chain": chain, "count": len(chain)}


def _trigger_downstream(task_id: str):
    """When a task completes, check if any dependent tasks can now be promoted."""
    dependents = _state.get_dependents(task_id)
    for dep_id in dependents:
        dep_task = _state.get_task(dep_id)
        if not dep_task or dep_task.status != TaskStatus.pending:
            continue
        # Check if all dependencies of this dependent are met
        all_done = all(
            (d := _state.get_task(d_id)) is not None
            and d.status == TaskStatus.completed
            for d_id in dep_task.depends_on
        )
        if all_done:
            _state.set_task_status(dep_id, TaskStatus.ready)
    # Then schedule any newly ready tasks
    _state.schedule_pending()


@router.post("/{task_id}/claim")
async def claim_task(task_id: str, req: ClaimTaskRequest):
    """Worker claims a task."""
    task = _state.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.status != TaskStatus.ready:
        raise HTTPException(status_code=409, detail=f"task is not claimable (status={task.status.value})")
    if task.assigned_to is not None:
        raise HTTPException(status_code=409, detail="task is already claimed")
    _state.set_task_status(task_id, TaskStatus.running)
    # Set assigned_to directly via task object
    with _state._tasks_lock:
        t = _state._tasks[task_id]
        t.assigned_to = req.node_id
        t.updated_at = __import__("datetime").datetime.utcnow()
        t.version += 1

    # Create lease
    if _lease_manager:
        _lease_manager.create(task_id=task_id, node_id=req.node_id)

    claimed_task = _state.get_task(task_id)
    return {
        **claimed_task.model_dump(),
        "claimed_at": claimed_task.updated_at.isoformat(),
    }


@router.post("/{task_id}/release")
async def release_task(task_id: str, req: ReleaseTaskRequest):
    """Worker releases a claimed task."""
    task = _state.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task.assigned_to != req.node_id:
        raise HTTPException(status_code=403, detail="task is not assigned to this node")

    # Revoke lease
    if _lease_manager:
        lease = _lease_manager.get_by_task(task_id)
        if lease:
            _lease_manager.revoke(lease.id)

    # S7 residual fix: honor the terminality guard — if set_task_status rejects the
    # transition (e.g. cancel_requested/cancelled), return 409 instead of 200.
    transitioned = _state.set_task_status(task_id, TaskStatus.ready)
    if not transitioned:
        raise HTTPException(
            status_code=409,
            detail=f"task is terminal (status={task.status.value}), cannot release to ready",
        )

    with _state._tasks_lock:
        t = _state._tasks[task_id]
        t.assigned_to = None
        t.updated_at = __import__("datetime").datetime.utcnow()
        t.version += 1
        if req.reason:
            t.fail_reason = req.reason
    released_task = _state.get_task(task_id)
    return released_task.model_dump()
