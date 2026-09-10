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
    model: str = "sonnet"
    poll_interval: float = 15.0  # seconds between poll cycles
    max_concurrent: int = 1  # max simultaneous spawns
    spawn_timeout: float = 1800.0  # max seconds per spawn (30 min)
    working_dir: str = ""  # working directory for spawned workers
    bdaya_dispatch_package: str = "@shared/bdaya-dispatch@latest"


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
    miss_count: int = 0  # consecutive polls where lane was absent from status
    spawn_exit_rc: Optional[int] = None  # set once spawn process exits
    spawn_exit_stderr: str = ""  # captured stderr tail on nonzero exit


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

    def _write_brief(self, task_id: str, title: str, description: str) -> Path:
        """Write the per-task brief file the guarded worker lane reads."""
        d = Path(self._config.working_dir or ".") / "hermes-briefs"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{task_id}.md"
        lines = [
            f"## Hermes cluster task {task_id}",
            "",
            f"**Title:** {title}",
            "",
        ]
        if description.strip():
            lines += [description.strip(), ""]
        lines += [
            "### Standing lane rules",
            "- You are a headless worker spawned by the Hermes cluster executor on node "
            f"`{self._node_id}`; report blockers in your RETURN VALUE, never AskUserQuestion.",
            "- NEVER approve or merge your own work; open MRs as Draft and hand off for independent review.",
            "- Cheap models only; never print a secret value.",
            "- When done, state exactly what you produced (files, MR links, proof) in your final message.",
            "",
        ]
        path.write_text(chr(10).join(lines), encoding="utf-8")
        return path

    def _spawn_worker(self, task: dict) -> None:
        """Spawn a bdaya-dispatch worker for the given task."""
        task_id = task.get("id", "")
        task_title = task.get("title", "")

        # Build the lane name (must be unique and traceable)
        lane_name = f"hermes-{task_id}"

        # bdaya-dispatch's `run` contract requires --goal (one line) TOGETHER with
        # --brief-file (an already-written file the lane reads); a bare --goal is
        # refused. The goal is the task title (one line); the brief carries the
        # full task text plus the standing lane rules.
        goal = " ".join(task_title.split())[:300] or task_id
        brief_path = self._write_brief(task_id, task_title, task.get("description") or "")

        # Spawn: npx -y -p @shared/bdaya-dispatch bdaya-dispatch run
        #   --name <lane_name> --goal <goal> --model <model>
        cmd = [
            "npx", "-y",
            "-p", self._config.bdaya_dispatch_package,
            "bdaya-dispatch", "run",
            "--name", lane_name,
            "--goal", goal,
            "--brief-file", str(brief_path),
            "--model", self._config.model,
            "--profile", self._config.profile,
            "--require-goal",
            "--force",
        ]

        # bdaya-dispatch resolves the CALLER's cpm profile from CLAUDE_CONFIG_DIR and
        # refuses outside a .claude-profiles root ("not a cpm multi-account machine").
        # Under a service that variable is unset, so point it at the executor's own
        # profile directory unless the operator already set it.
        env = dict(os.environ)
        env.setdefault(
            "CLAUDE_CONFIG_DIR",
            str(Path.home() / ".claude-profiles" / self._config.profile),
        )

        logger.info(
            "spawning worker for task %s: lane=%s goal=%s",
            task_id, lane_name, goal[:80],
        )

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=self._config.working_dir,
                env=env,
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

    # Terminal states per bdaya-dispatch contract (verified v3.63.7):
    #   done/completed → success; stopped/failed/missing/ambiguous → terminal failure.
    #   blocked is ACTIVE (blocked-self = waiting on subagent, blocked-human = needs input).
    _TERMINAL_DONE = frozenset({"done", "completed"})
    _TERMINAL_FAIL = frozenset({"stopped", "failed", "missing", "ambiguous"})

    # Registration grace: don't fail a lane for absence until this many
    # consecutive misses OR this many seconds have elapsed.
    _MISS_GRACE_COUNT = 4  # ~60s at default 15s poll_interval
    _MISS_GRACE_SECONDS = 90.0

    def _reap_finished_spawns(self) -> None:
        """Poll lane status for active spawns and report terminal states.

        bdaya-dispatch run backgrounds the lane and exits 0 immediately, so
        process exit codes are NOT useful for completion detection. Instead,
        query ``bdaya-dispatch status --json`` and inspect the lane state.

        Contract (verified against bdaya-dispatch v3.63.7):
          - done/completed → /complete
          - stopped/failed/missing/ambiguous → /fail
          - blocked/working/running/pending → keep waiting
          - spawn_timeout is outer bound, but a done lane observed late still completes
          - query failure (not just empty result) → keep waiting (bounded by timeout)
          - lane absent from status → grace window before failing
        """
        if not self._active_spawns:
            return

        lane_states = self._query_all_lane_statuses()

        # Also check spawn process exit for diagnostic info (F7)
        self._capture_spawn_exits()

        resolved = []  # (task_id, spawn, outcome, detail)
        with self._lock:
            for task_id, spawn in list(self._active_spawns.items()):
                elapsed = time.time() - spawn.started_at

                # 1. Check lane state FIRST (before timeout) — F3
                if lane_states is not None:
                    state = lane_states.get(spawn.lane_name)

                    if state is not None:
                        state_lower = state.lower()

                        # Reset miss counter on presence
                        spawn.miss_count = 0

                        if state_lower in self._TERMINAL_DONE:
                            detail = f"lane completed in {elapsed:.0f}s"
                            resolved.append((task_id, spawn, "done", detail))
                            continue
                        if state_lower in self._TERMINAL_FAIL:
                            detail = f"lane state={state} after {elapsed:.0f}s"
                            resolved.append((task_id, spawn, state_lower, detail))
                            continue
                        # else: active state (working/blocked/running/pending) → keep waiting

                    else:
                        # Lane absent from status — count misses with grace — F2
                        spawn.miss_count += 1

                        # If spawn process exited nonzero and lane never appeared,
                        # report spawn diagnostics — F7
                        if spawn.spawn_exit_rc is not None and spawn.spawn_exit_rc != 0:
                            detail = (
                                f"spawn exited rc={spawn.spawn_exit_rc}, "
                                f"lane never registered"
                            )
                            if spawn.spawn_exit_stderr:
                                detail += f": {spawn.spawn_exit_stderr[:300]}"
                            resolved.append((task_id, spawn, "spawn_failed", detail))
                            continue

                        # Grace window: don't fail until enough misses or time elapsed
                        if (spawn.miss_count >= self._MISS_GRACE_COUNT
                                and elapsed >= self._MISS_GRACE_SECONDS):
                            detail = (
                                f"lane not found after {spawn.miss_count} polls "
                                f"({elapsed:.0f}s)"
                            )
                            resolved.append((task_id, spawn, "missing", detail))
                            continue
                        # else: still within grace → keep waiting

                # 2. Timeout is outer bound for non-terminal lanes — F3
                if elapsed > self._config.spawn_timeout:
                    logger.warning(
                        "spawn timeout (%.0fs) for task %s (lane=%s)",
                        self._config.spawn_timeout,
                        task_id,
                        spawn.lane_name,
                    )
                    resolved.append((
                        task_id, spawn, "timeout",
                        f"exceeded {self._config.spawn_timeout:.0f}s",
                    ))
                    continue

                # 3. Query failure → keep waiting (bounded by timeout above)
                if lane_states is None:
                    logger.debug(
                        "status query failed, keeping spawn active: task=%s lane=%s",
                        task_id, spawn.lane_name,
                    )

        for task_id, spawn, outcome, detail in resolved:
            with self._lock:
                self._active_spawns.pop(task_id, None)

            if outcome == "done":
                self._report_completion(task_id, detail=detail)
            else:
                self._report_failure(task_id, f"lane {spawn.lane_name}: {detail}")

    def _capture_spawn_exits(self) -> None:
        """Check if any spawn processes have exited and capture diagnostics."""
        with self._lock:
            for spawn in self._active_spawns.values():
                if spawn.spawn_exit_rc is not None:
                    continue  # already captured
                rc = spawn.process.poll()
                if rc is not None:
                    spawn.spawn_exit_rc = rc
                    if rc != 0:
                        try:
                            stderr_data = ""
                            if spawn.process.stderr:
                                stderr_data = spawn.process.stderr.read() or ""
                            elif spawn.process.stdout:
                                stderr_data = spawn.process.stdout.read() or ""
                            if stderr_data:
                                lines = stderr_data.decode(errors="replace").strip().split("\n")
                                spawn.spawn_exit_stderr = "\n".join(lines[-5:])[:500]
                        except Exception:
                            pass

    def _query_all_lane_statuses(self) -> Optional[Dict[str, str]]:
        """Run ``bdaya-dispatch status --json`` and return {lane_name: state}.

        Returns ``None`` on query failure (timeout, unparseable, npx missing)
        so the caller can distinguish "no lanes" from "query broke".

        Parses stdout regardless of exit code — bdaya-dispatch exits 1 as a
        health alarm after printing valid JSON (F1).
        """
        cmd = [
            "npx", "-y",
            "-p", self._config.bdaya_dispatch_package,
            "bdaya-dispatch", "status", "--json",
        ]
        env = dict(os.environ)
        env.setdefault(
            "CLAUDE_CONFIG_DIR",
            str(Path.home() / ".claude-profiles" / self._config.profile),
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=self._config.working_dir or None,
                env=env,
            )
            # Parse stdout regardless of rc — rc=1 is a health alarm, not an error (F1)
            if not result.stdout or not result.stdout.strip():
                logger.warning(
                    "bdaya-dispatch status returned no output (rc=%d)",
                    result.returncode,
                )
                return None
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError as e:
                logger.warning("failed to parse bdaya-dispatch status JSON: %s", e)
                return None
            lanes_list = data.get("lanes")
            if not isinstance(lanes_list, list):
                logger.warning("bdaya-dispatch status 'lanes' is not a list")
                return None
            return {
                lane["lane"]: lane["state"]
                for lane in lanes_list
                if isinstance(lane, dict) and "lane" in lane and "state" in lane
            }
        except subprocess.TimeoutExpired:
            logger.warning("bdaya-dispatch status timed out")
            return None
        except FileNotFoundError:
            logger.warning("npx not found when querying lane status")
            return None

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

    def _report_completion(self, task_id: str, detail: str = "") -> None:
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
            logger.info("task %s marked completed: %s", task_id, detail or "ok")
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
