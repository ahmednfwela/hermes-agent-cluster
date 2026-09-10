"""Scheduler assigns the registry id (node_<id>); executor is configured with <id>. Both must match."""
from hermes_cluster.core.agent_executor import AgentExecutor


def _ex():
    ex = AgentExecutor.__new__(AgentExecutor)
    ex._node_id = "windows_pc_worker"
    return ex


def test_matches_bare_and_prefixed_ids():
    ex = _ex()
    assert ex._is_assigned_to_me("windows_pc_worker")
    assert ex._is_assigned_to_me("node_windows_pc_worker")


def test_rejects_other_nodes_and_empty():
    ex = _ex()
    assert not ex._is_assigned_to_me("node_macbook_worker")
    assert not ex._is_assigned_to_me("macbook_worker")
    assert not ex._is_assigned_to_me("")
    assert not ex._is_assigned_to_me(None)
