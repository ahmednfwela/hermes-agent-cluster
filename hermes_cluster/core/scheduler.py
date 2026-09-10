"""Fair, capacity-aware scheduling core (shared by in-memory + SQLite stores).

Single source of truth for *which* task goes to *which* node. Both
``ClusterState.schedule_pending`` and ``ClusterStore.schedule_pending``
used to run their own "first capability-matching node wins" loop, which
funelled every ready task to the first online node and left alternate
workers idle (#804, #833 — node_macbook_worker 32/32 while the Windows
nodes sat at 0/0).

Scheduling rule:

1. Only ONLINE nodes are candidates.
2. A READY task (priority ASC, then created_at ASC) goes to the candidate
   node with the fewest ACTIVE tasks; load ties break by least-recently
   picked (round-robin), so an identical-capability cluster spreads
   2/2/2 instead of N/0/0.
3. A node whose active count has reached its ``max_concurrent`` receives
   nothing. ``0`` means unlimited (matches ``NodeInfo.max_capacity``).
4. A task that still holds an ACTIVE lease is never (re-)assigned: a lease
   is exclusive ownership, and only recovery/expiry may move a task again.

A node's active count = tasks in ``running``/``assigned`` status whose
``assigned_to`` is that node.

The planner is pure: it returns assignments and the stores apply them
under their own lock/transaction. The only mutable state is the
round-robin tiebreak cursor, which each store owns per-instance.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import Node, TaskStatus

# Task states that occupy a node slot. ``running`` is what the scheduler
# sets on assignment; ``assigned`` exists in the Go enum and would count
# the same way if a store ever uses it.
ACTIVE_TASK_STATUSES = frozenset({TaskStatus.running, TaskStatus.assigned})

# Terminal states — a task in one of these must never be unassigned/revived.
TERMINAL_TASK_STATUSES = frozenset({
    TaskStatus.completed,
    TaskStatus.failed,
    TaskStatus.cancelled,
    TaskStatus.cancel_requested,
})

# 0 == unlimited (matches NodeInfo.max_capacity in models/__init__.py).
MAX_CONCURRENT_UNLIMITED = 0


def node_at_capacity(node: Node, active_count: int) -> bool:
    """True when *node* cannot accept another task under ``max_concurrent``.

    ``max_concurrent <= 0`` means unlimited (a node registered without a
    capacity is never considered full).
    """
    if node.max_concurrent <= 0:
        return False
    return active_count >= node.max_concurrent


def node_can_run(task_requires: List[str], node: Node) -> bool:
    """True when *node* declares every capability *task_requires*."""
    if not task_requires:
        return True
    return all(cap in node.capabilities for cap in task_requires)


class FairScheduler:
    """Planner that picks the least-loaded capable online node with spare capacity.

    Ties -> least-recently-picked, which gives round-robin rotation across
    an identical-capacity cluster. The cursor is per store instance so two
    clusters do not share rotation state.

    Methods in this class do not call back into the store, so a store may
    hold its own lock while planning without deadlock risk.
    """

    def __init__(self) -> None:
        self._pick_round: int = 0
        self._last_picked_at: Dict[str, int] = {}

    def choose(
        self,
        task_requires: List[str],
        online_nodes: List[Node],
        active_counts: Dict[str, int],
    ) -> Optional[Node]:
        """Pick the node for a task, or ``None`` when no candidate can take it.

        A candidate must (a) match the task's capabilities, and (b) not be
        at ``max_concurrent``. Among candidates the winner minimises
        ``(active_count, last_picked_round)`` — fewest active tasks first,
        round-robin on ties.
        """
        best: Optional[Node] = None
        best_key: Optional[tuple] = None
        for node in online_nodes:
            if not node_can_run(task_requires, node):
                continue
            active = active_counts.get(node.id, 0)
            if node_at_capacity(node, active):
                continue
            key = (active, self._last_picked_at.get(node.id, -1))
            if best_key is None or key < best_key:
                best_key = key
                best = node
        return best

    def mark_picked(self, node_id: str) -> None:
        """Record that *node_id* took the current tick (round-robin cursor)."""
        self._last_picked_at[node_id] = self._pick_round
        self._pick_round += 1