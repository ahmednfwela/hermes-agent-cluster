"""The spawn must carry the profile, --force, and a CLAUDE_CONFIG_DIR the bdaya-dispatch harness accepts."""
import os
from pathlib import Path
from hermes_cluster.core import agent_executor as ae


class _Cfg:
    bdaya_dispatch_package = "@shared/bdaya-dispatch@latest"
    model = "sonnet"
    profile = "alibaba1"
    working_dir = os.getcwd()


class _FakeProc:
    pid = 4242
    def poll(self): return None


def _run_spawn(monkeypatch):
    captured = {}
    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd; captured["kw"] = kw; return _FakeProc()
    monkeypatch.setattr(ae.subprocess, "Popen", fake_popen)
    ex = ae.AgentExecutor.__new__(ae.AgentExecutor)
    ex._config = _Cfg(); ex._node_id = "windows_pc_worker"
    for attr, val in (("_spawns", {}), ("_active", {}), ("_lock", None)):
        if not hasattr(ex, attr):
            try: setattr(ex, attr, val)
            except Exception: pass
    try:
        ex._spawn_worker({"id": "task_x", "title": "do a thing"})
    except AttributeError:
        pass  # bookkeeping after Popen may touch state we did not construct; Popen args are what we test
    return captured


def test_spawn_passes_profile_and_force(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    c = _run_spawn(monkeypatch)
    cmd = c["cmd"]
    assert "--profile" in cmd and cmd[cmd.index("--profile") + 1] == "alibaba1"
    assert "--force" in cmd
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert "@shared/bdaya-dispatch@latest" in cmd


def test_spawn_sets_claude_config_dir_when_unset(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    c = _run_spawn(monkeypatch)
    env = c["kw"]["env"]
    assert env["CLAUDE_CONFIG_DIR"] == str(Path.home() / ".claude-profiles" / "alibaba1")


def test_spawn_keeps_operator_claude_config_dir(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", r"C:\custom\.claude-profiles\lead")
    c = _run_spawn(monkeypatch)
    assert c["kw"]["env"]["CLAUDE_CONFIG_DIR"] == r"C:\custom\.claude-profiles\lead"
