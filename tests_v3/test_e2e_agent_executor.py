"""End-to-end test for the agent executor lifecycle.

Uses a mock subprocess (echo command that exits 0) to prove the full loop:
  submit task -> schedule/assign -> executor picks it up -> "spawn" completes -> task marked completed

This avoids needing a real bdaya-dispatch spawn (which would cost credits and require
a live worker restart). The real spawn is validated by the unit tests mocking subprocess.Popen.

Run:
    pytest tests_v3/test_e2e_agent_executor.py -v
    python tests_v3/test_e2e_agent_executor.py
"""

import os
import sys
import time
import json
import threading
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

# Fix Windows console encoding
os.environ["PYTHONIOENCODING"] = "utf-8"

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_e2e_lifecycle():
    """Full E2E: main + worker with mock spawn, verify task lifecycle."""
    from hermes_cluster.app import create_app
    from hermes_cluster.core.agent_executor import AgentExecutor, AgentExecutorConfig
    import hermes_cluster.core.agent_executor as ae_module

    print("=== Agent Executor E2E Test ===")
    print()

    # We'll use FastAPI TestClient for both main and worker
    # But we need the worker_connector and agent_executor to actually run background threads
    # So we'll create the apps and manually drive the executor

    # 1. Create main app and its state
    from hermes_cluster.state import ClusterState
    from hermes_cluster.core.node_manager import NodeManager
    from hermes_cluster.lease.lease_manager import LeaseManager

    main_state = ClusterState()
    main_state.cluster_id = "e2e-test"
    main_state.node_id = "e2e-main"
    main_state.node_role = "main"

    main_nm = NodeManager(main_state)
    main_lm = LeaseManager(main_state)
    main_lm.start()
    main_nm.start_watchdog()

    # Wire routers
    from hermes_cluster.routers import nodes as nodes_mod
    from hermes_cluster.routers import tasks as tasks_mod
    from hermes_cluster.routers import leases as leases_mod
    from hermes_cluster.routers import schedule as schedule_mod
    from hermes_cluster.routers import status as status_mod

    nodes_mod.init(main_state, node_manager=main_nm)
    tasks_mod.init(main_state, lease_manager=main_lm)
    leases_mod.init(main_state)
    schedule_mod.init(main_state)
    status_mod.init(main_state)

    main_state._node_manager = main_nm
    main_state._lease_manager = main_lm

    from fastapi.testclient import TestClient
    from hermes_cluster.app import create_app

    main_app = create_app(
        cluster_id="e2e-test",
        node_id="e2e-main",
        node_role="main",
    )
    main_client = TestClient(main_app)
    print("[OK] Main node created")

    # 2. Register the worker node on main
    join_resp = main_client.post("/api/v1/nodes/join", json={
        "node_name": "e2e-worker",
        "capabilities": ["tooling"],
    })
    worker_node_id = join_resp.json()["node_id"]
    print(f"[OK] Worker registered: {worker_node_id}")

    # 3. Submit a trivial task
    task_resp = main_client.post("/api/v1/tasks", json={
        "title": "E2E test: report the current timestamp",
        "requires": ["tooling"],
        "priority": 1,
    })
    task = task_resp.json()
    task_id = task["id"]
    print(f"[OK] Task submitted: {task_id} (status={task['status']})")

    # 4. Trigger scheduler to assign task to worker
    sched_resp = main_client.post("/api/v1/schedule/trigger", json={})
    sched = sched_resp.json()
    print(f"[OK] Scheduler: promoted={sched['promoted']}, scheduled={sched['scheduled']}")

    # Check task is now running + assigned
    task_check = main_client.get("/api/v1/tasks").json()
    running_task = next((t for t in task_check if t["id"] == task_id), None)
    print(f"[OK] Task after schedule: status={running_task['status']}, assigned_to={running_task.get('assigned_to')}")

    if running_task["status"] != "running" or not running_task.get("assigned_to"):
        # Manually claim the task (scheduler may not have assigned to our worker)
        print("[..] Manually claiming task for worker...")
        claim_resp = main_client.post(f"/api/v1/tasks/{task_id}/claim", json={
            "node_id": worker_node_id,
        })
        print(f"[OK] Claimed: status={claim_resp.json().get('status')}")

    # 5. Now test the agent_executor's claim_and_spawn + reap cycle
    # We mock subprocess.Popen to use a fast command instead of bdaya-dispatch
    original_popen = subprocess.Popen

    def mock_popen(cmd, **kwargs):
        if any("bdaya-dispatch" in str(c) for c in cmd):
            return original_popen(
                [sys.executable, "-c", "import time; time.sleep(1); print('task done')"],
                **kwargs,
            )
        return original_popen(cmd, **kwargs)

    ae_module.subprocess.Popen = mock_popen
    original_signed_request = ae_module._signed_request

    # Create an executor that talks to main_client's API
    # But the executor uses urlopen, not TestClient. So we need a real HTTP server.
    # Instead, let's mock the _signed_request to use main_client directly.

    executor = AgentExecutor(
        config=AgentExecutorConfig(
            enabled=True,
            profile="test",
            model="test-model",
            poll_interval=2,
            max_concurrent=1,
        ),
        node_id=worker_node_id,
        cluster_endpoint="http://unused",  # won't be used since we mock requests
    )

    # Mock _signed_request to route through main_client
    def mock_signed_request(endpoint, method, path, data, token, node_id, timeout=15):
        if method == "GET":
            resp = main_client.get(path)
        elif method == "POST":
            resp = main_client.post(path, json=data or {})
        else:
            return None
        if resp.status_code >= 400:
            print(f"    [WARN] {method} {path} -> {resp.status_code}: {resp.text[:200]}")
            return None
        return resp.json()

    ae_module._signed_request = mock_signed_request

    print()
    print("[..] Running executor poll cycle...")

    # 6. Run one poll cycle — should find the task and spawn
    executor._poll_once()
    print(f"[OK] After poll: active_spawns={executor.active_count}")

    # 7. Wait for the mock spawn to finish (1 second)
    time.sleep(3)

    # 8. Run another poll cycle — should reap the finished spawn and report completion
    executor._poll_once()

    # 9. Check final task state — ASSERT the outcome (B1 fix)
    final_tasks = main_client.get("/api/v1/tasks").json()
    final_task = next((t for t in final_tasks if t["id"] == task_id), None)
    print(f"[OK] Final task state: status={final_task['status']}")

    # Restore
    ae_module.subprocess.Popen = original_popen
    ae_module._signed_request = original_signed_request

    main_lm.stop()
    main_nm.stop_watchdog()

    # CRITICAL ASSERTIONS — mutation "remove _report_completion" must fail here
    assert final_task is not None, "Task disappeared from state"
    assert final_task["status"] == "completed", (
        f"Task should be completed, got {final_task['status']}"
    )

    print()
    print("=== E2E TEST PASSED ===")
    print(f"Task {task_id} went: pending -> ready -> running -> completed")
    print("Executor spawned mock worker, reaped it, reported completion.")


if __name__ == "__main__":
    try:
        test_e2e_lifecycle()
        print("\nAll assertions passed.")
        sys.exit(0)
    except AssertionError as e:
        print(f"\nAssertion failed: {e}")
        sys.exit(1)
