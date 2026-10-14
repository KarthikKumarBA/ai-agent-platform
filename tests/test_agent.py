"""Agent unit tests: no LLM calls and no Kubernetes cluster needed."""

import json
import sys
import uuid
from pathlib import Path

import pytest
from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

import actions  # noqa: E402
import diagnose  # noqa: E402
import remediate  # noqa: E402
import server  # noqa: E402


# ---------- Helpers ----------
def make_pod(name: str, ready: bool) -> dict:
    return {
        "pod": name,
        "phase": "Running",
        "node": "test-node",
        "containers": [{
            "name": "sample-app",
            "ready": ready,
            "restart_count": 0,
            "state": "running",
            "waiting_reason": None,
            "terminated_reason": None,
            "running_for_seconds": 120,
            "last_termination_reason": None,
            "image": "sample-app:test",
        }],
    }


def make_diagnosis(action: str) -> dict:
    return {
        "status": "down" if action != "none" else "healthy",
        "summary": "test summary",
        "evidence": ["test evidence"],
        "root_cause": "test root cause",
        "confidence": "high",
        "recommended_action": action,
        "reason": "test reason",
    }


# ---------- actions.py ----------
def test_allowlist_blocks_unknown_action():
    result = actions.execute_action("delete_namespace", "sample-app", "sample-app")
    assert result["executed"] is False
    assert "allowlist" in result["reason"]


def test_restart_dry_run_does_not_call_kubernetes(monkeypatch):
    monkeypatch.setattr(actions, "_apps_api", lambda: pytest.fail("Kubernetes API was called"))
    result = actions.restart_deployment("sample-app", "sample-app", dry_run=True)
    assert result == {"action": "restart_deployment", "executed": False, "dry_run": True}


# ---------- diagnose.py ----------
def test_focus_pod_prefers_unhealthy_pod():
    pods = [make_pod("healthy-pod", True), make_pod("broken-pod", False)]
    assert diagnose._pick_focus_pod(pods)["pod"] == "broken-pod"


def test_focus_pod_falls_back_to_first_pod():
    pods = [make_pod("pod-a", True), make_pod("pod-b", True)]
    assert diagnose._pick_focus_pod(pods)["pod"] == "pod-a"


# ---------- server.py watcher ----------
def test_watcher_detects_unhealthy():
    assert server._is_unhealthy([]) is True
    assert server._is_unhealthy([make_pod("p", False)]) is True
    assert server._is_unhealthy([make_pod("p", True)]) is False


# ---------- remediate.py graph ----------
@pytest.fixture
def fake_agent(monkeypatch, tmp_path):
    """Replace the cluster and LLM with fakes; returns a runner and the executed-actions log."""
    executed = []

    monkeypatch.setattr(remediate, "AUDIT_FILE", tmp_path / "audit.jsonl")
    monkeypatch.setattr(remediate, "collect_node",
                        lambda state: {"signals": {"pods": [make_pod("p-old", False)]}})
    monkeypatch.setattr(remediate, "wait_for_healthy",
                        lambda ns, deploy: {"healthy": True, "seconds": 1.0, "ready": "1/1"})
    monkeypatch.setattr(remediate, "get_pod_status",
                        lambda ns, selector: [make_pod("p-new", True)])

    def fake_execute(action, ns, deploy, dry_run=False):
        executed.append(action)
        return {"action": action, "executed": not dry_run, "dry_run": dry_run}

    monkeypatch.setattr(remediate, "execute_action", fake_execute)

    def run(action: str, answer: str | None = None, dry_run: bool = False):
        monkeypatch.setattr(remediate, "analyze_node", lambda state: {
            "diagnosis": make_diagnosis(action), "usage": {"total_tokens": 10}})

        app = remediate.build_graph()
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        app.invoke({
            "namespace": "sample-app", "selector": "app=sample-app",
            "deployment": "sample-app", "dry_run": dry_run,
        }, config)

        interrupted = bool(app.get_state(config).next)
        if interrupted and answer is not None:
            app.invoke(Command(resume=answer), config)
        return interrupted, app.get_state(config).values

    return run, executed, tmp_path / "audit.jsonl"


def test_graph_approved_restart_executes_and_verifies(fake_agent):
    run, executed, audit_file = fake_agent
    interrupted, state = run("restart_deployment", answer="y")

    assert interrupted is True
    assert state["approval"] == "approved"
    assert state["action_result"]["executed"] is True
    assert state["verification"]["healthy"] is True
    assert executed == ["restart_deployment"]

    record = json.loads(audit_file.read_text().strip())
    assert record["approval"] == "approved"


def test_graph_rejected_restart_does_nothing(fake_agent):
    run, executed, _ = fake_agent
    interrupted, state = run("restart_deployment", answer="n")

    assert interrupted is True
    assert state["approval"] == "rejected"
    assert "action_result" not in state
    assert executed == []


def test_graph_dry_run_does_not_change_cluster(fake_agent):
    run, executed, _ = fake_agent
    _, state = run("restart_deployment", answer="y", dry_run=True)

    assert state["action_result"]["executed"] is False
    assert state["action_result"]["dry_run"] is True
    assert "verification" not in state


def test_graph_investigate_needs_no_approval(fake_agent):
    run, executed, audit_file = fake_agent
    interrupted, state = run("investigate")

    assert interrupted is False
    assert "approval" not in state
    assert executed == []
    assert audit_file.exists()
