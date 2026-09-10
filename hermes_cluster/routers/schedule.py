"""Schedule endpoints — /api/v1/schedule"""

from fastapi import APIRouter

from ..state import ClusterState

router = APIRouter(prefix="/api/v1/schedule", tags=["schedule"])

_state: ClusterState = None


def init(state: ClusterState):
    global _state
    _state = state


@router.post("/trigger")
async def schedule_trigger():
    """Trigger scheduler to assign ready tasks to idle nodes.

    ``assignments`` contains ONLY the assignments this call created — it no
    longer re-lists tasks that were already running (which made the endpoint
    read like a lease-expiry re-queue every time it was hit; #833).
    """
    promoted = _state.trigger_pending_tasks()
    assignments = _state.schedule_pending_detailed()

    return {
        "promoted": promoted,
        "scheduled": len(assignments),
        "assignments": assignments,
    }


@router.get("/stats")
async def schedule_stats():
    return _state.get_schedule_stats()


@router.get("/decisions")
async def schedule_decisions():
    decisions = _state.get_decisions()
    return {"decisions": decisions, "count": len(decisions)}
