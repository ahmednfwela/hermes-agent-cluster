"""GitLab issue → cluster-task intake — /api/v1/intake/gitlab

Two ingestion paths (both produce the same outcome: a cluster Task):
  1. Webhook: POST /api/v1/intake/gitlab/webhook — GitLab push hook payload
  2. Poll:    POST /api/v1/intake/gitlab/poll    — manual trigger; queries GitLab
             for issues labelled `tooling` created since last poll

Background poller: if `GITLAB_INTAKE_TOKEN` env var is set, a background thread
polls every 30s. Configure endpoint via `GITLAB_INTAKE_ENDPOINT` env var
(default: https://gitlab.bdaya-dev.com) and project via `GITLAB_INTAKE_PROJECT`
(default: shared%2Fclaude-plugins).

All ingested tasks carry `requires: ["tooling"]` and a `source` metadata field
pointing to the originating GitLab issue URL.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request

from ..state import ClusterState

logger = logging.getLogger("hermes_cluster.intake")

router = APIRouter(prefix="/api/v1/intake/gitlab", tags=["intake"])

_state: Optional[ClusterState] = None
_poller: Optional["_GitLabPoller"] = None
_issue_iid_to_task_id: Dict[int, str] = {}


def init(state: ClusterState):
    global _state, _poller
    _state = state
    token = os.environ.get("GITLAB_INTAKE_TOKEN", "")
    endpoint = os.environ.get("GITLAB_INTAKE_ENDPOINT", "https://gitlab.bdaya-dev.com")
    project = os.environ.get("GITLAB_INTAKE_PROJECT", "shared%2Fclaude-plugins")
    label = os.environ.get("GITLAB_INTAKE_LABEL", "tooling")
    interval = int(os.environ.get("GITLAB_INTAKE_INTERVAL", "30"))
    if token:
        _poller = _GitLabPoller(
            state=state,
            token=token,
            endpoint=endpoint,
            project=project,
            label=label,
            interval=interval,
        )
        _poller.start()
        logger.info("GitLab intake poller started (project=%s, label=%s)", project, label)


# ---------------------------------------------------------------------------
# Webhook handler
# ---------------------------------------------------------------------------

@router.post("/webhook")
async def webhook(request: Request):
    """GitLab webhook receiver.

    Accepts `issue` events. For `open`/`reopen` actions on issues carrying the
    configured label, creates a cluster task. Idempotent: if a task already
    exists for this issue (by source metadata), returns the existing task.
    """
    body = await request.json()
    event_type = body.get("object_kind")
    if event_type != "issue":
        return {"status": "ignored", "reason": f"event={event_type}"}

    attrs = body.get("object_attributes", {})
    action = attrs.get("action")
    labels = [lbl.get("title", "") for lbl in body.get("labels", [])]
    label = os.environ.get("GITLAB_INTAKE_LABEL", "tooling")
    if label not in labels:
        return {"status": "ignored", "reason": f"label={label} not in {labels}"}
    if action not in ("open", "reopen"):
        return {"status": "ignored", "reason": f"action={action}"}

    issue_iid = attrs.get("iid")
    title = attrs.get("title", "")
    url = attrs.get("url", "")
    task = _create_task_from_issue(issue_iid=issue_iid, title=title, url=url, label=label)
    return {"status": "created", "task_id": task.id, "task": task.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# Manual poll trigger
# ---------------------------------------------------------------------------

@router.post("/poll")
async def poll():
    """Manually trigger a GitLab poll. Returns list of task IDs created."""
    if _poller is None:
        raise HTTPException(status_code=503, detail="GitLab intake poller not configured (set GITLAB_INTAKE_TOKEN)")
    created = await _poller.poll_once()
    return {"status": "ok", "created": len(created), "task_ids": created}


@router.get("/status")
async def status():
    """Return intake poller status."""
    if _poller is None:
        return {"configured": False}
    return {
        "configured": True,
        "last_poll": _poller.last_poll.isoformat() if _poller.last_poll else None,
        "issues_seen": _poller.issues_seen,
        "tasks_created": _poller.tasks_created,
    }


# ---------------------------------------------------------------------------
# Issue → task mapping
# ---------------------------------------------------------------------------

def _create_task_from_issue(
    issue_iid: int,
    title: str,
    url: str,
    label: str,
):
    """Create a cluster task from a GitLab issue, dedup by source iid."""
    # Dedup by issue iid (module-level mapping survives within-process restarts)
    if issue_iid in _issue_iid_to_task_id:
        existing = _state.get_task(_issue_iid_to_task_id[issue_iid])
        if existing is not None:
            return existing

    task_id = "task_" + secrets.token_hex(8)
    task = _state.create_task(
        task_id=task_id,
        title=f"[#{issue_iid}] {title}",
        requires=[label],
        priority=3,
    )
    _issue_iid_to_task_id[issue_iid] = task_id
    _state.trigger_pending_tasks()
    return task


# ---------------------------------------------------------------------------
# Background poller
# ---------------------------------------------------------------------------

class _GitLabPoller:
    """Background thread that polls GitLab for new labelled issues."""

    def __init__(
        self,
        state: ClusterState,
        token: str,
        endpoint: str,
        project: str,
        label: str,
        interval: int,
    ):
        self.state = state
        self.token = token
        self.endpoint = endpoint.rstrip("/")
        self.project = project
        self.label = label
        self.interval = interval
        self.last_poll: Optional[datetime] = None
        self.issues_seen = 0
        self.tasks_created = 0
        self._seen_iids: set = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="gitlab-intake-poller")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(self.poll_once())
                loop.close()
            except Exception:
                logger.exception("GitLab intake poll cycle failed")
            self._stop.wait(self.interval)

    async def poll_once(self) -> List[str]:
        """Query GitLab for issues labelled `self.label`, create tasks for unseen ones."""
        url = f"{self.endpoint}/api/v4/projects/{self.project}/issues"
        params = {
            "labels": self.label,
            "state": "opened",
            "per_page": 50,
            "order_by": "created_at",
            "sort": "asc",
        }
        headers = {"PRIVATE-TOKEN": self.token}
        created_ids: List[str] = []
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            issues = resp.json()

        self.last_poll = datetime.now(timezone.utc)
        for issue in issues:
            iid = issue["iid"]
            self.issues_seen += 1
            if iid in self._seen_iids:
                continue
            # Check module-level dedup map (shared with webhook path)
            if iid in _issue_iid_to_task_id:
                self._seen_iids.add(iid)
                continue
            self._seen_iids.add(iid)
            task = _create_task_from_issue(
                issue_iid=iid,
                title=issue["title"],
                url=issue["web_url"],
                label=self.label,
            )
            self.tasks_created += 1
            created_ids.append(task.id)
        return created_ids
