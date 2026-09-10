"""Agent executor — bridge from cluster task lease to real bdaya worker run.

Polls the main node for tasks assigned (status=running, assigned_to=this node),
spawns a guarded headless Claude Code worker via bdaya-dispatch for each task,
and reports completion/failure back to the cluster via signed API calls.

Design:
  - Reuses the proven bdaya-dispatch spawn mechanism (NOT a new agent runner)
  - Inherits bdaya-defaults plugin, hooks, mandate gates, typed tooling
  - One active spawn per lease at a time (configurable max_concurrent)
  - Honours lease TTL — renews while the spawn is running
  - Crashed/hung spawn → /fail with reason, never a silent hang
  - Config-driven (profile, model), zero hardcoded client specifics
  - Uses the same peer-token signing as worker_connector.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class AgentExecutorConfig:
    """Configuration for the agent executor."""
    enabled: bool = False
    profile: str = "alibaba1"
    model: str = "qwen3.7-plus"
    poll_interval: float = 15.0  # seconds between poll cycles
    max_concurrent: int = 1  # max simultaneous spawns
    spawn_timeout: float = 1800.0  # max seconds per spawn (30 min)
    working_dir: str = ""  # working directory for spawned workers
    bdaya_dispatch_package: str = "@shared/bdaya-dispatch"


# ---------------------------------------------------------------------------
# Active spawn tracking
# ---------------------------------------------------------------------------

@dataclass
class ActiveSpawn:
    """Tracks a running bdaya-dispatch subprocess."""
    task_id: str
    task_title: str
    process: subprocess.Popen
    lease_id: str = ""
    started_at: float = 0.0
    lane_name: str = ""


# ---------------------------------------------------------------------------
# Signing (same algorithm as worker_connector.py)
# ---------------------------------------------------------------------------

def _resolve_peer_token(explicit: str = "") -> str:
    """Resolve the peer token for signing outbound requests."""
    env_token = os.environ.get("PEER_TOKEN", "")
    if env_token:
        return env_token
    token_path = Path.home() / ".config" / "bdaya" / "hermes-peer-token"
    if token_path.is_file():
        return token_path.read_text().strip()
    if explicit:
        return explicit
    return ""


def _sign_request(
    token: str, node_id: str, method: str, path: str, body: bytes
) -> Dict[str, str]:
    """Sign a request with HMAC-SHA256, returning auth headers."""
    if not token:
        return {}
    ts = int(time.time())
    body_hash = hashlib.sha256(body).hexdigest()
    message = f"{node_id}:{ts}:{method}:{path}:{body_hash}"
    signature = hmac.new(
        token.encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    return {
        "X-Peer-Node": node_id,
        "X-Peer-Timestamp": str(ts),
        "X-Peer-Signature": signature,
    }


def _signed_request(
    endpoint: str,
    method: str,
    path: str,
    data: Optional[dict],
    token: str,
    node_id: str,
    timeout: int = 15,
) -> Optional[dict]:
    """Send a signed JSON request to the main node."""
    url = f"{endpoint}{path}"
    body = json.dumps(data).encode() if data else b""
    req = Request(url, data=body if data else None, method=method)
    req.add_header("Content-Type", "application/json")
    headers = _sign_request(token, node_id, method, path, body)
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except URLError as e:
        logger.warning("signed %s %s failed: %s", method, path, e)
        return None
    except Exception as e:
        logger.warning("signed %s %s error: %s", method, path, e)
        return None


# ---------------------------------------------------------------------------
# AgentExecutor
# ---------------------------------------------------------------------------

class AgentExecutor:
    """Polls for leased tasks, spawns bdaya workers, reports results.

    Lifecycle:
        1. Construct with config, node_id, cluster_endpoint
        2. Call start() to launch the background poll thread
        3. Call stop() for clean shutdown
    """

    def __init__(
        self,
        config: AgentExecutorConfig,
        node_id: str,
        cluster_endpoint: str,
        peer_token: str = "",
    ):
        self._config = config
        self._node_id = node_id
        self._cluster_endpoint = cluster_endpoint.rstrip("/")
        self._token = _resolve_peer_token(peer_token)

        self._active_spawns: Dict[str, ActiveSpawn] = {}  # task_id -> spawn
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False

        # Resolve working directory
        if not self._config.working_dir:
            # Default: D:\projects\devops-aggregate (the aggregate repo)
            default_dir = Path(r"D:\projects\devops-aggregate")
            if default_dir.exists():
                self._config.working_dir = str(default_dir)
            else:
                self._config.working_dir = str(Path.cwd())

    def start(self) -> None:
        """Start the background poll + spawn thread."""
        if self._running:
            logger.warning("agent executor already running")
            return
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="agent-executor",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "agent executor started: node=%s profile=%s model=%s max_concurrent=%d",
            self._node_id,
            self._config.profile,
            self._config.model,
            self._config.max_concurrent,
        )

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the executor and wait for the thread to finish."""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        logger.info("agent executor stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active_spawns)

    def status(self) -> dict:
        """Return executor status for diagnostics."""
        with self._lock:
            spawns = []
            for tid, spawn in self._active_spawns.items():
                spawns.append({
                    "task_id": tid,
                    "task_title": spawn.task_title[:80],
                    "lane_name": spawn.lane_name,
                    "lease_id": spawn.lease_id,
                    "running_seconds": round(time.time() - spawn.started_at, 1),
                    "pid": spawn.process.pid,
                    "poll_alive": spawn.process.poll() is None,
                })
            return {
                "running": self._running,
                "node_id": self._node_id,
                "profile": self._config.profile,
                "model": self._config.model,
                "max_concurrent": self._config.max_concurrent,
                "active_spawns": len(spawns),
                "spawns": spawns,
            }

    # -------------------------------------------------------------------
    # Main poll loop
    # -------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """Main loop: poll for tasks, manage spawns, report results."""
        logger.info("agent executor poll loop started")
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception:
                logger.exception("error in agent executor poll cycle")
            self._stop_event.wait(timeout=self._config.poll_interval)
        logger.info("agent executor poll loop ended")

    def _poll_once(self) -> None:
        """Single poll cycle: check results, find new tasks, renew leases."""
        # 1. Check completed/failed spawns
        self._reap_finished_spawns()

        # 2. Renew leases for active spawns
        self._renew_leases()

        # 3. Find and spawn new tasks if capacity allows
        with self._lock:
            available_slots = self._config.max_concurrent - len(self._active_spawns)
        if available_slots > 0:
            self._claim_and_spawn(available_slots)

    # -------------------------------------------------------------------
    # Task discovery + spawning
    # -------------------------------------------------------------------


    def _is_assigned_to_me(self, assigned_to: str) -> bool:
        """The main registers nodes as ``node_<id>`` (nodes.py join) while the
        executor is configured with the bare ``<id>``; accept both spellings so a
        task the scheduler assigned to this node is actually claimed (live bug:
        cut-over task stuck ``running`` on every node, #804)."""
        if not assigned_to:
            return False
        mine = {self._node_id, f"node_{self._node_id}"}
        return assigned_to in mine or assigned_to.removeprefix("node_") == self._node_id

    def _claim_and_spawn(self, max_spawns: int) -> None:
        """Poll main for assigned tasks and spawn workers for them."""
        # GET /api/v1/tasks from main node
        tasks = _signed_request(
            self._cluster_endpoint,
            "GET",
            "/api/v1/tasks",
            None,
            self._token,
            self._node_id,
        )
        if tasks is None:
            logger.warning("failed to fetch tasks from main")
            return

        # Filter: status=running, assigned_to=this node, not already spawning
        with self._lock:
            active_task_ids = set(self._active_spawns.keys())

        candidates = []
        for task in tasks:
            task_id = task.get("id", "")
            status = task.get("status", "")
            assigned_to = task.get("assigned_to", "")
            if (
                status == "running"
                and self._is_assigned_to_me(assigned_to)
                and task_id not in active_task_ids
            ):
                candidates.append(task)

        # Sort by priority (lower number = higher priority)
        candidates.sort(key=lambda t: t.get("priority", 3))

        # Spawn up to max_spawns
        for task in candidates[:max_spawns]:
            self._spawn_worker(task)

    def _spawn_worker(self, task: dict) -> None:
        """Spawn a bdaya-dispatch worker for the given task."""
        task_id = task.get("id", "")
        task_title = task.get("title", "")

        # Build the lane name (must be unique and traceable)
        lane_name = f"hermes-{task_id}"

        # Build the goal prompt
        goal = task_title

        # Spawn: npx -y -p @shared/bdaya-dispatch bdaya-dispatch run
        #   --name <lane_name> --goal <goal> --model <model>
        cmd = [
            "npx", "-y",
            "-p", self._config.bdaya_dispatch_package,
            "bdaya-dispatch", "run",
            "--name", lane_name,
            "--goal", goal,
            "--model", self._config.model,
            "--require-goal",
        ]

        logger.info(
            "spawning worker for task %s: lane=%s goal=%s",
            task_id, lane_name, goal[:80],
        )

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=self._config.working_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                # On Windows, create a new process group so we can kill the tree
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                if os.name == "nt"
                else 0,
            )
        except FileNotFoundError:
            logger.error(
                "npx not found — cannot spawn bdaya-dispatch. "
                "Ensure Node.js and npm are on PATH."
            )
            self._report_failure(task_id, "executor_error: npx not found on PATH")
            return
        except Exception as e:
            logger.error("failed to spawn worker for task %s: %s", task_id, e)
            self._report_failure(task_id, f"executor_error: {e}")
            return

        # Find the lease for this task (to track for renewal)
        lease_id = self._find_lease_for_task(task_id)

        spawn = ActiveSpawn(
            task_id=task_id,
            task_title=task_title,
            process=proc,
            lease_id=lease_id,
            started_at=time.time(),
            lane_name=lane_name,
        )

        with self._lock:
            self._active_spawns[task_id] = spawn

        logger.info(
            "spawned worker: task=%s pid=%d lane=%s",
            task_id, proc.pid, lane_name,
        )

    # -------------------------------------------------------------------
    # Reaping finished spawns
    # -------------------------------------------------------------------

    def _reap_finished_spawns(self) -> None:
        """Check if any spawned processes have exited and report results."""
        finished = []
        with self._lock:
            for task_id, spawn in self._active_spawns.items():
                rc = spawn.process.poll()
                if rc is not None:
                    finished.append((task_id, spawn, rc))
                elif time.time() - spawn.started_at > self._config.spawn_timeout:
                    # Timeout — kill the process
                    logger.warning(
                        "spawn timeout (%.0fs) for task %s, killing pid %d",
                        self._config.spawn_timeout,
                        task_id,
                        spawn.process.pid,
                    )
                    try:
                        spawn.process.kill()
                    except OSError:
                        pass
                    finished.append((task_id, spawn, -1))

        for task_id, spawn, rc in finished:
            with self._lock:
                self._active_spawns.pop(task_id, None)

            if rc == 0:
                self._report_completion(task_id)
            else:
                # Try to capture last lines of output for the failure reason
                reason = f"worker exited with code {rc}"
                try:
                    if spawn.process.stdout:
                        tail = spawn.process.stdout.read()
                        if tail:
                            lines = tail.decode(errors="replace").strip().split("\n")
                            tail_text = "\n".join(lines[-5:])  # last 5 lines
                            reason = f"worker exited with code {rc}: {tail_text[:500]}"
                except Exception:
                    pass
                self._report_failure(task_id, reason)

    # -------------------------------------------------------------------
    # Lease renewal
    # -------------------------------------------------------------------

    def _renew_leases(self) -> None:
        """Extend leases for active spawns to prevent TTL expiry."""
        with self._lock:
            spawns_with_leases = [
                (tid, s) for tid, s in self._active_spawns.items()
                if s.lease_id
            ]

        for task_id, spawn in spawns_with_leases:
            result = _signed_request(
                self._cluster_endpoint,
                "POST",
                f"/api/v1/leases/{spawn.lease_id}/extend",
                {},
                self._token,
                self._node_id,
            )
            if result:
                logger.debug("lease renewed: task=%s lease=%s", task_id, spawn.lease_id)
            else:
                logger.warning(
                    "lease renewal failed: task=%s lease=%s — may expire",
                    task_id, spawn.lease_id,
                )

    def _find_lease_for_task(self, task_id: str) -> str:
        """Find the active lease ID for a task from the main node."""
        leases = _signed_request(
            self._cluster_endpoint,
            "GET",
            "/api/v1/leases",
            None,
            self._token,
            self._node_id,
        )
        if leases is None:
            return ""
        for lease in leases:
            if (
                lease.get("task_id") == task_id
                and lease.get("node_id") == self._node_id
                and lease.get("status") == "active"
            ):
                return lease.get("id", "")
        return ""

    # -------------------------------------------------------------------
    # Reporting results back to the cluster
    # -------------------------------------------------------------------

    def _report_completion(self, task_id: str) -> None:
        """Mark a task as completed on the main node."""
        result = _signed_request(
            self._cluster_endpoint,
            "POST",
            f"/api/v1/tasks/{task_id}/complete",
            {},
            self._token,
            self._node_id,
        )
        if result:
            logger.info("task %s marked completed", task_id)
        else:
            logger.error("failed to mark task %s completed", task_id)

    def _report_failure(self, task_id: str, reason: str) -> None:
        """Mark a task as failed on the main node."""
        result = _signed_request(
            self._cluster_endpoint,
            "POST",
            f"/api/v1/tasks/{task_id}/fail",
            {"reason": reason},
            self._token,
            self._node_id,
        )
        if result:
            logger.info("task %s marked failed: %s", task_id, reason[:100])
        else:
            logger.error("failed to mark task %s failed", task_id)
