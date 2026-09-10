"""Agent executor — bridge from cluster task lease to a real worker run.

Polls the main node for tasks assigned (status=running, assigned_to=this node),
spawns a guarded headless worker for each task, and reports completion/failure
back to the cluster via signed API calls.

Two spawn modes (config ``agent_executor.worker``; default ``bdaya-dispatch``):
  - ``bdaya-dispatch`` — the proven npx bdaya-dispatch lane; completion is
    detected by polling ``bdaya-dispatch status --json`` (the run backgrounds
    instantly, so process exit codes are NOT the completion signal).
  - ``hermes`` — a NATIVE non-interactive hermes session
    (``hermes -p <profile> chat --query-file <brief> -Q``) that runs the task
    to completion, writes a result file, and exits non-zero on failure; this
    mode tracks the process (pid + exit code + result file) instead of lane
    status.

Restart safety (#804 note 132791): the task→lane map is persisted in the
ClusterStore SQLite (``task_spawns`` table) and reconciled on start — a lane
spawned before a crash is resumed from its record, and a task is re-spawned
only when no record exists (no duplicate worker after an executor restart).

Stateful lanes (shared/claude-plugins#847/#833, owner ruling 2026-09-10): a
task carrying ``lane_key`` joins a named stateful lane. The first task with a
new ``lane_key`` spawns a hermes session titled by the lane_key (``-c
<lane_key> --create-if-missing``); every later task with that lane_key RESUMES
the lane's live session (``--resume <session_id>``) and delivers its brief as
the next message — one hermes session per lane, cheap models (qwen3.8-flash
profile default for authors), no per-task cold starts. The lane→session map
lives in the ``lanes`` table (lane_key, session_id, profile, role, node,
created_at, last_task_id) and is re-attached on worker restart by lane_key.
Reviewer lanes (``role="reviewer"``) spawn with ``-m <hermes_reviewer_model>``
(default qwen3.7-plus, the opus tier) so the merge gate accepts their verdicts.

Design:
  - Worker-mode aware spawn + reap, one active spawn per lease at a time
  - Honours lease TTL — renews while the spawn is running
  - Crashed/hung spawn → /fail with reason, never a silent hang
  - Config-driven (worker, profile, model, hermes_profile, hermes_bin)
  - Uses the same peer-token signing as worker_connector.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import shutil
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
    worker: str = "bdaya-dispatch"  # "bdaya-dispatch" | "hermes"
    poll_interval: float = 15.0  # seconds between poll cycles
    max_concurrent: int = 1  # max simultaneous spawns
    spawn_timeout: float = 1800.0  # max seconds per spawn (30 min)
    # Max idle seconds before a STATEFUL LANE is reaped (its lanes row cleared,
    # hermes session left intact on disk). ``spawn_timeout`` bounds a single
    # DELIVERY; ``lane_idle_timeout`` bounds how long a lane may sit idle with
    # no delivery running. Default 6 hours (six-hour default per the lead
    # requirement, shared/claude-plugins#833 2026-09-10).
    lane_idle_timeout: float = 21600.0
    working_dir: str = ""  # working directory for spawned workers
    hermes_profile: str = "default"  # hermes -p/--profile (hermes worker mode)
    hermes_bin: str = ""  # path to the hermes CLI; empty → resolve at spawn
    # Reviewer lanes pass the opus-tier model explicitly so the merge gate
    # accepts their verdicts; author lanes use the profile default (no -m).
    hermes_reviewer_model: str = "qwen3.7-plus"
    bdaya_dispatch_package: str = "@shared/bdaya-dispatch@latest"


# ---------------------------------------------------------------------------
# Active spawn tracking
# ---------------------------------------------------------------------------

@dataclass
class ActiveSpawn:
    """Tracks a running spawn (bdaya-dispatch subprocess or native hermes)."""
    task_id: str
    task_title: str
    process: subprocess.Popen
    lease_id: str = ""
    started_at: float = 0.0
    lane_name: str = ""
    mode: str = "bdaya-dispatch"  # which worker mode spawned this (matches config.worker)
    result_path: str = ""  # hermes mode: path of the result file the lane writes
    result_file: Optional[object] = None  # open handle for the result file (hermes)
    stderr_path: str = ""  # hermes mode: path of the child's stderr log
    stderr_file: Optional[object] = None  # open handle for the stderr log (hermes)
    resumed: bool = False  # True when reconstructed from the persisted spawn map
    lane_key: str = ""  # stateful lane identity this delivery belongs to
    role: str = "author"  # author (profile default model) | reviewer (opus tier)
    session_id: str = ""  # hermes session id this lane maps to (lanes table)
    miss_count: int = 0  # consecutive polls where lane was absent from status
    spawn_exit_rc: Optional[int] = None  # set once spawn process exits
    spawn_exit_stderr: str = ""  # captured stderr tail on nonzero exit


class _ResumedProcess:
    """Minimal process stand-in for a spawn reconciled from the persisted map.

    The real subprocess handle is lost when the executor restarts mid-task, so
    a resumed spawn gets this instead: it answers ``pid`` and ``poll()`` like a
    Popen (``poll()`` → None while we believe the process may still be alive,
    or a nonzero rc once we know it cannot be). Completion of a resumed lane is
    then driven by the lane-status/result-file logic, exactly like a live one.
    """

    def __init__(self, pid: int):
        self.pid = pid
        self._exited = False

    def poll(self) -> Optional[int]:
        if self._exited:
            return 0
        try:
            os.kill(self.pid, 0)
            return None
        except ProcessLookupError:
            self._exited = True
            return 0
        except PermissionError:
            return None
        except Exception:
            # os.kill can raise on invalid pid kinds; treat as unknown → alive
            return None


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
        store: Optional[object] = None,
    ):
        self._config = config
        self._node_id = node_id
        self._cluster_endpoint = cluster_endpoint.rstrip("/")
        self._token = _resolve_peer_token(peer_token)
        # Optional ClusterStore/ClusterState for the persisted task->lane map
        # (#804 note 132791). Without a store the executor is stateless —
        # restart will re-spawn (superseded once a store is wired).
        self._store = store

        self._active_spawns: Dict[str, ActiveSpawn] = {}  # task_id -> spawn
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._reconciled = False
        # Persisted task-id cache (F4): reconciled once from the store, then
        # maintained in-memory by _persist_spawn/_drop_persisted_spawn so the
        # poll loop never re-reads SQLite. Reseeded by reconcile on start.
        self._persisted_ids: set = set()

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
        self._reconcile_persisted_spawns()
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
                    "mode": spawn.mode,
                    "resumed": spawn.resumed,
                    "running_seconds": round(time.time() - spawn.started_at, 1),
                    "pid": spawn.process.pid,
                    "poll_alive": spawn.process.poll() is None,
                    "result_path": spawn.result_path,
                })
            return {
                "running": self._running,
                "node_id": self._node_id,
                "profile": self._config.profile,
                "model": self._config.model,
                "worker": getattr(self._config, "worker", "bdaya-dispatch"),
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

        # 1b. Reap idle stateful lanes (lead requirement, #833 2026-09-10):
        # a lane idle past lane_idle_timeout has its lanes row cleared so the
        # next task with that lane_key starts fresh (session left on disk).
        self._reap_idle_lanes()

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
        active_task_ids |= self._persisted_spawn_task_ids()

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

    def _write_brief(
        self,
        task_id: str,
        title: str,
        description: str,
        lane_key: str = "",
        role: str = "author",
    ) -> Path:
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
        if lane_key:
            lines += [
                f"**Stateful lane:** `{lane_key}` (**{role}** role)",
                "You are a continuing lane: this task is the next delivery in an existing "
                "lane session. Read the lane's prior context (you are resuming it), and "
                "answer THIS brief as the next message. Keep the lane's thread and "
                "decisions consistent.",
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
        """Spawn a worker for the given task, dispatching by ``worker`` mode."""
        # getattr guards the spawn_env unit tests which construct a bare _Cfg
        # (no worker attr) → default to the legacy bdaya-dispatch mode.
        if getattr(self._config, "worker", "bdaya-dispatch") == "hermes":
            self._spawn_hermes_worker(task)
        else:
            self._spawn_bdaya_worker(task)

    def _spawn_bdaya_worker(self, task: dict) -> None:
        """Spawn a bdaya-dispatch worker for the given task."""
        task_id = task.get("id", "")
        task_title = task.get("title", "")
        lane_key = task.get("lane_key", "") or ""
        role = task.get("role", "author") or "author"

        # Build the lane name (must be unique and traceable)
        lane_name = f"hermes-{task_id}"

        # bdaya-dispatch's `run` contract requires --goal (one line) TOGETHER with
        # --brief-file (an already-written file the lane reads); a bare --goal is
        # refused. The goal is the task title (one line); the brief carries the
        # full task text plus the standing lane rules.
        goal = " ".join(task_title.split())[:300] or task_id
        brief_path = self._write_brief(
            task_id, task_title, task.get("description") or "",
            lane_key=lane_key, role=role,
        )

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
            mode="bdaya-dispatch",
            lane_key=lane_key,
            role=role,
        )

        with self._lock:
            self._active_spawns[task_id] = spawn
        self._persist_spawn(spawn)

        logger.info(
            "spawned worker: task=%s pid=%d lane=%s",
            task_id, proc.pid, lane_name,
        )

    def _resolve_hermes_bin(self) -> str:
        """Resolve the hermes CLI binary for the hermes worker mode."""
        if self._config.hermes_bin:
            return self._config.hermes_bin
        # Shebang launcher installed by the official install script.
        candidates = [
            str(Path.home() / ".local" / "bin" / "hermes"),
            str(Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes"),
        ]
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return shutil.which("hermes") or "hermes"

    def _hermes_result_path(self, task_id: str) -> Path:
        """Directory/result file a native hermes lane writes on completion."""
        d = Path(self._config.working_dir or ".") / "hermes-results"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{task_id}.result.md"

    def _hermes_stderr_path(self, task_id: str) -> Path:
        """Directory/log file where a native hermes lane's stderr is captured.

        The session id line (``session_id: <id>``) is written to stderr on every
        exit (cli.py:4087), including success — so stderr is captured to a file,
        not a PIPE, and parsed on reap (F2). A PIPE would also deadlock a chatty
        child that floods stderr beyond the buffer while never being drained.
        """
        d = Path(self._config.working_dir or ".") / "hermes-results"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{task_id}.stderr.log"

    def _delivery_model_flag(self, role: str) -> List[str]:
        """Role → model mapping for a native hermes delivery.

        Author lanes use the profile default (no ``-m``); reviewer lanes pass
        the opus-tier model so the merge gate accepts their verdicts. The ``-m/``
        ``--model`` flag is accepted both top-level and on ``chat``
        (hermes_cli/_parser.py:126-128, :211-212); ``qwen3.7-plus`` is the
        opus-tier catalog entry (hermes_cli/models_catalog_static.py:139).
        """
        if role == "reviewer" and self._config.hermes_reviewer_model:
            return ["-m", self._config.hermes_reviewer_model]
        return []

    def _spawn_hermes_worker(self, task: dict) -> None:
        """Spawn a NATIVE non-interactive hermes session for the given task.

        Invocation (verified against hermes_cli on this node — see brief):
          hermes -p <profile> chat --query-file <brief> -Q [-m <reviewer model>]
        - ``chat --query-file`` (hermes_cli/_parser.py:194-202) reads the single
          query from a file byte-for-byte (no shell quoting), runs to
          completion, and exits.
        - ``-Q/--quiet`` (hermes_cli/_parser.py:225-226) suppresses the banner/
          spinner/tool previews; only the final response and session info are
          printed, and cli.py exits 0 on success / non-zero on failure
          (cli.py:4089-4101).
        - ``-p <profile>`` (hermes_cli/main.py:508-559) is consumed before
          parsing and sets HERMES_HOME for the chosen profile.

        Stateful lanes (task.lane_key): a task whose lane_key already has a
        live lane RESUMES that hermes session — ``--resume <session_id>``
        (hermes_cli/_parser.py:227-229; history restored into conversation by
        ``cli.setup_mixin._preload_resumed_session`` at cli.py:3633/cli_agent_setup_mixin.py:616-660,
        then the query-file is delivered as the next user message —
        cli.py:4060). Only a NEW lane_key spawns a fresh session (``-c
        <lane_key> --create-if-missing``, hermes_cli/_parser.py:235-241 /
        main.py:1359-1397). Session ids are captured from stderr on reap and
        recorded in the ``lanes`` table.

        Track: pid + exit code + result file (NOT bdaya-dispatch lane status).
        """
        task_id = task.get("id", "")
        task_title = task.get("title", "")
        lane_key = task.get("lane_key", "") or ""
        role = task.get("role", "author") or "author"

        lane_name = f"hermes-{task_id}"
        brief_path = self._write_brief(
            task_id, task_title, task.get("description") or "",
            lane_key=lane_key, role=role,
        )
        result_path = self._hermes_result_path(task_id)
        stderr_path = self._hermes_stderr_path(task_id)

        # Stateful lane resolution: resume an existing live lane's session,
        # else this is the lane's first delivery (fresh titled session).
        resume_session_id = ""
        if lane_key:
            lane = self._store.get_lane(lane_key) if self._store else None
            if lane and lane.get("session_id"):
                resume_session_id = lane["session_id"]
                logger.info(
                    "lane %s has live session %s — resuming it for task %s",
                    lane_key, resume_session_id, task_id,
                )

        hermes_bin = self._resolve_hermes_bin()
        cmd = [
            hermes_bin,
            "-p", self._config.hermes_profile,
            "chat",
            "--query-file", str(brief_path),
            "-Q",
        ]
        cmd[3:3] = self._delivery_model_flag(role)
        if resume_session_id:
            cmd.extend(["--resume", resume_session_id])
        elif lane_key:
            # First delivery of this lane: create a session titled lane_key so
            # it can be resumed by name/id later (hermes_cli/main.py:1359-1397).
            cmd.extend(["-c", lane_key, "--create-if-missing"])

        logger.info(
            "spawning native hermes worker for task %s: %s",
            task_id, " ".join(cmd),
        )

        # Hold the result + stderr files open for the child's lifetime (closing
        # the parent handle too early breaks stdout inheritance on Windows).
        result_file = None
        stderr_file = None
        try:
            result_file = open(result_path, "w", encoding="utf-8")
            stderr_file = open(stderr_path, "w", encoding="utf-8")
            proc = subprocess.Popen(
                cmd,
                cwd=self._config.working_dir,
                # Agent final response (stdout) lands in the result file;
                # stderr (incl. the session_id line) goes to its own log file.
                stdout=result_file,
                stderr=stderr_file,
                # On Windows, create a new process group so we can kill the tree
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                if os.name == "nt"
                else 0,
            )
        except FileNotFoundError:
            for f in (result_file, stderr_file):
                if f is not None:
                    try:
                        f.close()
                    except Exception:
                        pass
            logger.error(
                "hermes not found — cannot spawn native worker (looked for %s). "
                "Ensure hermes is installed or set agent_executor.hermes_bin.",
                hermes_bin,
            )
            self._report_failure(task_id, "executor_error: hermes not found on PATH")
            return
        except Exception as e:
            for f in (result_file, stderr_file):
                if f is not None:
                    try:
                        f.close()
                    except Exception:
                        pass
            logger.error("failed to spawn native hermes worker for task %s: %s", task_id, e)
            self._report_failure(task_id, f"executor_error: {e}")
            return

        lease_id = self._find_lease_for_task(task_id)

        spawn = ActiveSpawn(
            task_id=task_id,
            task_title=task_title,
            process=proc,
            lease_id=lease_id,
            started_at=time.time(),
            lane_name=lane_name,
            mode="hermes",
            result_path=str(result_path),
            result_file=result_file,
            stderr_path=str(stderr_path),
            stderr_file=stderr_file,
            lane_key=lane_key,
            role=role,
            session_id=resume_session_id,
            resumed=bool(resume_session_id),
        )

        with self._lock:
            self._active_spawns[task_id] = spawn
        self._persist_spawn(spawn)

        logger.info(
            "spawned native hermes worker: task=%s pid=%d result=%s",
            task_id, proc.pid, result_path,
        )

    # -------------------------------------------------------------------
    # Persisted task->lane map (#804 note 132791)
    # -------------------------------------------------------------------

    def _persist_spawn(self, spawn: ActiveSpawn) -> None:
        """Persist a spawn record so a mid-task restart does not re-spawn."""
        if getattr(self, "_store", None) is None:
            return
        try:
            self._persisted_ids.add(spawn.task_id)
            self._store.record_task_spawn(
                task_id=spawn.task_id,
                mode=spawn.mode,
                job_id=spawn.lane_name,
                pid=spawn.process.pid,
                started_at=spawn.started_at,
                lease_id=spawn.lease_id,
                lane_name=spawn.lane_name,
                result_path=spawn.result_path,
                lane_key=spawn.lane_key,
                role=spawn.role,
                session_id=spawn.session_id,
            )
            # Reflect the lane in the stateful-lanes table whenever a lane_key
            # exists (session_id filled in later, on reap, from stderr).
            if spawn.lane_key:
                self._store.record_lane(
                    lane_key=spawn.lane_key,
                    session_id=spawn.session_id,
                    profile=self._config.hermes_profile,
                    role=spawn.role,
                    node=self._node_id,
                    last_task_id=spawn.task_id,
                )
        except Exception:
            logger.exception("failed to persist spawn record for task %s", spawn.task_id)

    def _persisted_spawn_task_ids(self) -> set:
        """Task ids with a persisted spawn record (live spawns must not re-spawn).

        Served from the in-memory cache (F4) — the store is read once at
        reconcile, then kept in sync by _persist_spawn/_drop_persisted_spawn.
        """
        return set(self._persisted_ids)

    def _reconcile_persisted_spawns(self) -> None:
        """Re-load the persisted task->lane map into the in-memory spawn table.

        On restart mid-task, the executor must resume tracking the lanes it
        already spawned — NOT spawn new ones. Records are rehydrated into
        ActiveSpawn with a process stand-in; completion is still detected by
        the mode-specific reap logic (lane status / hermes result file).
        """
        if getattr(self, "_store", None) is None:
            self._reconciled = True
            return
        if self._reconciled:
            return
        try:
            records = self._store.get_all_task_spawns()
        except Exception:
            logger.exception("failed to reconcile persisted spawn records")
            self._reconciled = True
            return
        reconstituted = 0
        # Reseed the persisted-id cache from the store (F4) so the poll loop
        # never re-reads SQLite per cycle.
        self._persisted_ids = {r.get("task_id", "") for r in records} - {""}
        with self._lock:
            for record in records:
                task_id = record.get("task_id", "")
                if not task_id or task_id in self._active_spawns:
                    continue
                lane_key = record.get("lane_key") or ""
                role = record.get("role") or "author"
                session_id = record.get("session_id") or ""
                # Worker-restart re-attach by lane_key: if the record does not
                # carry a session id but the lanes table knows the lane's
                # session, recover it so a resume (not a fresh spawn) follows.
                if lane_key and not session_id and getattr(self, "_store", None) is not None:
                    try:
                        lane = self._store.get_lane(lane_key)
                        if lane:
                            session_id = lane.get("session_id") or ""
                    except Exception:
                        logger.exception("failed to read lane %s during reconcile", lane_key)
                spawn = ActiveSpawn(
                    task_id=task_id,
                    task_title=record.get("job_id") or task_id,
                    process=_ResumedProcess(int(record.get("pid") or 0)),
                    lease_id=record.get("lease_id") or "",
                    started_at=float(record.get("started_at") or time.time()),
                    lane_name=record.get("lane_name") or f"hermes-{task_id}",
                    mode=record.get("mode") or "bdaya-dispatch",
                    result_path=record.get("result_path") or "",
                    stderr_path=(
                        record.get("stderr_path")
                        or str(self._hermes_stderr_path(task_id))
                        if record.get("mode") == "hermes"
                        else record.get("stderr_path") or ""
                    ),
                    resumed=True,
                    lane_key=lane_key,
                    role=role,
                    session_id=session_id,
                )
                self._active_spawns[task_id] = spawn
                reconstituted += 1
                if lane_key:
                    # Block a duplicate worker for the lane's task (the spawn is
                    # live-tracked again, not re-delivered to the same lane).
                    logger.info(
                        "reattached lane %s (session %s) to task %s after restart",
                        lane_key, session_id or "?", task_id,
                    )
        self._reconciled = True
        if reconstituted:
            logger.info(
                "reconciled %d persisted spawn(s) from store — resuming tracking, "
                "NOT re-spawning", reconstituted,
            )

    def _drop_persisted_spawn(self, task_id: str) -> None:
        """Remove a terminal spawn's record so a future run may spawn again."""
        if getattr(self, "_store", None) is None:
            return
        try:
            self._persisted_ids.discard(task_id)
            self._store.delete_task_spawn(task_id)
        except Exception:
            logger.exception("failed to drop persisted spawn record for task %s", task_id)

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

        # Native hermes lanes are tracked by process exit + result file, not by
        # bdaya-dispatch lane status — skip the npx status query if every active
        # spawn is a hermes worker.
        have_bdaya_lane = any(
            s.mode != "hermes" for s in self._active_spawns.values()
        )
        lane_states = self._query_all_lane_statuses() if have_bdaya_lane else None

        # Also check spawn process exit for diagnostic info (F7)
        self._capture_spawn_exits()

        resolved = []  # (task_id, spawn, outcome, detail)
        with self._lock:
            for task_id, spawn in list(self._active_spawns.items()):
                elapsed = time.time() - spawn.started_at

                # Hermes workers: reap by pid + exit code + result file.
                if spawn.mode == "hermes":
                    self._reap_hermes_spawn(task_id, spawn, elapsed, resolved)
                    continue

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
            # Stateful lane: capture the hermes session id from this delivery's
            # stderr into the lanes table BEFORE the record is dropped, so the
            # next task with the same lane_key resumes that session.
            if outcome == "done" and spawn.mode == "hermes":
                self._touch_lane_from_spawn(spawn, task_id)
            # A terminal lane's persisted record is dropped so a future run of
            # the same task may spawn again; an active lane's record survives
            # restarts and blocks a duplicate spawn.
            self._drop_persisted_spawn(task_id)

            if outcome == "done":
                self._report_completion(task_id, detail=detail)
            else:
                self._report_failure(task_id, f"lane {spawn.lane_name}: {detail}")

    def _reap_hermes_spawn(
        self,
        task_id: str,
        spawn: ActiveSpawn,
        elapsed: float,
        resolved: List,
    ) -> None:
        """Reap a native hermes spawn by its process exit + result file.

        Completion contract (hermes ``chat --query-file ... -Q``):
          - RC 0 and a non-empty result file → done
          - RC 0 without a result file → fail (agent produced nothing)
          - RC != 0 → fail with captured stderr tail
          - still running → keep waiting (bounded by spawn_timeout)
        A resumed spawn (executor restarted mid-task) drives completion off the
        result file: if it exists and is non-empty the lane finished writing
        even though the original process handle is gone.
        """
        rc = spawn.process.poll()

        # A resolved hermes spawn releases its result/stderr handles (the child
        # has exited by then, so the close only releases the parent's copies).
        if rc is not None:
            for attr in ("result_file", "stderr_file"):
                fh = getattr(spawn, attr, None)
                if fh is not None:
                    try:
                        fh.flush()
                        fh.close()
                    except Exception:
                        pass
                    finally:
                        setattr(spawn, attr, None)

        # Result file is the primary completion signal for hermes: the agent
        # writes its final response there, then exits 0.
        result_ok = bool(spawn.result_path) and Path(spawn.result_path).is_file()
        if result_ok:
            try:
                contents = Path(spawn.result_path).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                contents = ""
            if contents.strip():
                resolved.append((
                    task_id, spawn, "done",
                    f"hermes result written in {elapsed:.0f}s ({spawn.result_path})",
                ))
                return

        if rc is not None and rc != 0:
            detail = f"hermes exited rc={rc} after {elapsed:.0f}s"
            if spawn.spawn_exit_stderr:
                detail += f": {spawn.spawn_exit_stderr[:300]}"
            resolved.append((task_id, spawn, "spawn_failed", detail))
            return

        if rc is not None and rc == 0:
            # Exited cleanly but produced no result file/content.
            resolved.append((
                task_id, spawn, "no_result",
                f"hermes exited rc=0 after {elapsed:.0f}s but wrote no result",
            ))
            return

        # Timeout is the outer bound for a still-running lane. Kill the child so
        # a timed-out hermes run doesn't keep burning quota as an orphan (F1).
        if rc is None and elapsed > self._config.spawn_timeout:
            logger.warning(
                "hermes spawn timeout (%.0fs) for task %s — killing pid %s",
                self._config.spawn_timeout, task_id, getattr(spawn.process, "pid", "?"),
            )
            self._kill_spawn_process(spawn)
            resolved.append((
                task_id, spawn, "timeout",
                f"exceeded {self._config.spawn_timeout:.0f}s",
            ))

    def _kill_spawn_process(self, spawn: ActiveSpawn) -> None:
        """Best-effort terminate of a spawn's child process (tree on Windows)."""
        proc = spawn.process
        if proc is None or isinstance(proc, _ResumedProcess):
            return  # nothing to kill (reconciled record holds no live handle)
        try:
            if os.name == "nt":
                proc.kill()
            else:
                proc.terminate()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _capture_spawn_exits(self) -> None:
        """Check if any spawn processes have exited and capture diagnostics."""
        with self._lock:
            for spawn in self._active_spawns.values():
                if spawn.spawn_exit_rc is not None:
                    continue  # already captured
                rc = spawn.process.poll()
                if rc is not None:
                    spawn.spawn_exit_rc = rc
                    # Hermes stderr now lives in a per-spawn log file (F2), so
                    # read it for diagnostics AND for the session_id line.
                    if rc != 0 or spawn.mode == "hermes":
                        try:
                            stderr_data = self._read_spawn_stderr(spawn)
                            if stderr_data:
                                lines = stderr_data.strip().split("\n")
                                spawn.spawn_exit_stderr = "\n".join(lines[-5:])[:500]
                        except Exception:
                            pass

    def _read_spawn_stderr(self, spawn: ActiveSpawn) -> str:
        """Read a spawn's stderr: from the log file (hermes) or the PIPE."""
        if getattr(spawn, "stderr_path", ""):
            try:
                return Path(spawn.stderr_path).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                return ""
        try:
            if spawn.process.stderr:
                data = spawn.process.stderr.read() or b""
            elif spawn.process.stdout:
                data = spawn.process.stdout.read() or b""
            else:
                return ""
            if isinstance(data, bytes):
                return data.decode(errors="replace")
            return str(data)
        except Exception:
            return ""

    def _extract_session_id(self, stderr_text: str) -> str:
        """Parse the ``session_id: <id>`` line hermes prints to stderr (cli.py:4063,4087).

        Lines look like ``session_id: 20260910_120000_ab12cd`` or
        ``session_id: <id>``; return the id or '' when absent. Best-effort,
        never raises.
        """
        if not stderr_text:
            return ""
        for line in stderr_text.splitlines():
            line = line.strip()
            if line.startswith("session_id:"):
                val = line.split(":", 1)[1].strip()
                if val:
                    return val
        return ""

    def _touch_lane_from_spawn(self, spawn: ActiveSpawn, task_id: str) -> None:
        """Record a lane's session id + last task into the lanes table.

        Called on reap for a hermes delivery: read the stderr log for hermes'
        ``session_id`` line and persist it, so the next task with the same
        lane_key resumes that session (``--resume <session_id>``).
        """
        if not spawn.lane_key or getattr(self, "_store", None) is None:
            return
        try:
            stderr_text = self._read_spawn_stderr(spawn)
            session_id = self._extract_session_id(stderr_text)
            self._store.record_lane(
                lane_key=spawn.lane_key,
                session_id=session_id or spawn.session_id,
                profile=self._config.hermes_profile,
                role=spawn.role,
                node=self._node_id,
                last_task_id=task_id,
            )
            if session_id and session_id != spawn.session_id:
                spawn.session_id = session_id
                logger.info(
                    "lane %s now bound to hermes session %s (task %s)",
                    spawn.lane_key, session_id, task_id,
                )
        except Exception:
            logger.exception("failed to record lane session for lane %s", spawn.lane_key)

    def _reap_idle_lanes(self) -> int:
        """Reap stateful lanes idle past ``lane_idle_timeout``.

        A lane is idle when NO delivery is currently running for it AND its
        last activity (``last_active_at``, falling back to ``created_at``) is
        older than ``agent_executor.lane_idle_timeout`` (default 6 h). Reaping
        clears the ``lanes`` ROW only — the hermes session itself is left
        intact on disk (``--resume``/``-c`` can still re-attach) — so the next
        task with that lane_key starts fresh. Returns the number of lanes
        reaped.
        """
        store = getattr(self, "_store", None)
        if store is None or not getattr(store, "get_all_lanes", None):
            return 0
        timeout = getattr(self._config, "lane_idle_timeout", 21600.0)
        if timeout <= 0:
            return 0
        try:
            lanes = store.get_all_lanes()
        except Exception:
            logger.exception("failed to list lanes during idle reap")
            return 0

        # A lane with an in-flight delivery is NOT idle — keep it.
        with self._lock:
            busy_lane_keys = {
                s.lane_key for s in self._active_spawns.values() if s.lane_key
            }

        reaped = 0
        now = time.time()
        for lane in lanes:
            lane_key = lane.get("lane_key", "")
            if not lane_key or lane_key in busy_lane_keys:
                continue
            last_active = lane.get("last_active_at") or lane.get("created_at") or 0
            try:
                idle_seconds = now - float(last_active)
            except (TypeError, ValueError):
                idle_seconds = float("inf")
            if idle_seconds <= timeout:
                continue
            try:
                if store.delete_lane(lane_key):
                    reaped += 1
                    logger.info(
                        "reaped idle stateful lane %s (idle %.0fs > %.0fs) — "
                        "lanes row cleared, hermes session left on disk",
                        lane_key, idle_seconds, timeout,
                    )
            except Exception:
                logger.exception("failed to reap idle lane %s", lane_key)
        return reaped

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
