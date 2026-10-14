"""Agent unit tests: no LLM calls and no Kubernetes cluster needed."""

import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

import actions  # noqa: E402
import diagnose  # noqa: E402
import remediate  # noqa: E402
import server  # noqa: E402
from redact import redact_env, redact_text  # noqa: E402


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


# ---------- Rollback with a fake Kubernetes API ----------
REV = actions.REVISION_ANNOTATION


def fake_replica_set(revision: int, image: str, owner_uid: str = "deploy-uid"):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            annotations={REV: str(revision)},
            owner_references=[SimpleNamespace(uid=owner_uid)],
        ),
        spec=SimpleNamespace(template=SimpleNamespace(
            metadata=SimpleNamespace(labels={"app": "sample-app",
                                             "pod-template-hash": f"hash{revision}"}),
            spec=SimpleNamespace(containers=[SimpleNamespace(image=image)]),
        )),
    )


class FakeAppsApi:
    def __init__(self, current_revision: int, replica_sets: list):
        self.deployment = SimpleNamespace(
            metadata=SimpleNamespace(annotations={REV: str(current_revision)}, uid="deploy-uid"),
            spec=SimpleNamespace(
                selector=SimpleNamespace(match_labels={"app": "sample-app"}),
                template=None,
            ),
        )
        self.replica_sets = replica_sets
        self.replaced = None

    def read_namespaced_deployment(self, name, namespace):
        return self.deployment

    def list_namespaced_replica_set(self, namespace, label_selector=None):
        return SimpleNamespace(items=self.replica_sets)

    def replace_namespaced_deployment(self, name, namespace, body):
        self.replaced = body


def test_rollback_dry_run_picks_previous_revision(monkeypatch):
    api = FakeAppsApi(3, [fake_replica_set(1, "app:v1"), fake_replica_set(2, "app:v2"),
                          fake_replica_set(3, "app:v3")])
    monkeypatch.setattr(actions, "_apps_api", lambda: api)

    result = actions.rollback_deployment("sample-app", "sample-app", dry_run=True)

    assert result["from_revision"] == 3
    assert result["to_revision"] == 2
    assert result["image"] == "app:v2"
    assert result["executed"] is False
    assert api.replaced is None


def test_rollback_apply_restores_previous_template(monkeypatch):
    api = FakeAppsApi(3, [fake_replica_set(2, "app:v2"), fake_replica_set(3, "app:v3")])
    monkeypatch.setattr(actions, "_apps_api", lambda: api)

    result = actions.rollback_deployment("sample-app", "sample-app", dry_run=False)

    assert result["executed"] is True
    template = api.replaced.spec.template
    assert template.spec.containers[0].image == "app:v2"
    assert "pod-template-hash" not in template.metadata.labels


def test_rollback_without_previous_revision(monkeypatch):
    api = FakeAppsApi(1, [fake_replica_set(1, "app:v1")])
    monkeypatch.setattr(actions, "_apps_api", lambda: api)

    result = actions.rollback_deployment("sample-app", "sample-app", dry_run=False)

    assert result["executed"] is False
    assert "No previous revision" in result["reason"]
    assert api.replaced is None


def test_rollback_ignores_replica_sets_of_other_deployments(monkeypatch):
    api = FakeAppsApi(2, [fake_replica_set(1, "other:v1", owner_uid="other-uid"),
                          fake_replica_set(2, "app:v2")])
    monkeypatch.setattr(actions, "_apps_api", lambda: api)

    result = actions.rollback_deployment("sample-app", "sample-app", dry_run=True)

    assert result["executed"] is False
    assert "No previous revision" in result["reason"]


# ---------- redact.py ----------
def test_redact_text_removes_secrets():
    text = (
        "connecting with password=hunter2 token: abc123 "
        "key sk-abcdefghijklmnopqrstuvwx and AKIAABCDEFGHIJKLMNOP "
        "Authorization: Bearer eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl"
    )
    result = redact_text(text)

    assert "hunter2" not in result
    assert "abc123" not in result
    assert "sk-abcdefghijklmnopqrstuvwx" not in result
    assert "AKIAABCDEFGHIJKLMNOP" not in result
    assert "eyJhbGciOi" not in result
    assert redact_text("INFO: Health check passed") == "INFO: Health check passed"


def test_redact_env_hides_secret_names():
    env = {"ENVIRONMENT": "kubernetes", "DB_PASSWORD": "hunter2", "API_KEY": "xyz"}
    result = redact_env(env)

    assert result["ENVIRONMENT"] == "kubernetes"
    assert result["DB_PASSWORD"] == "[REDACTED]"
    assert result["API_KEY"] == "[REDACTED]"


# ---------- diagnose.py ----------
def test_focus_pod_prefers_unhealthy_pod():
    pods = [make_pod("healthy-pod", True), make_pod("broken-pod", False)]
    assert diagnose._pick_focus_pod(pods)["pod"] == "broken-pod"


def test_focus_pod_falls_back_to_first_pod():
    pods = [make_pod("pod-a", True), make_pod("pod-b", True)]
    assert diagnose._pick_focus_pod(pods)["pod"] == "pod-a"


# ---------- server.py ----------
def test_watcher_detects_unhealthy():
    assert server._is_unhealthy([]) is True
    assert server._is_unhealthy([make_pod("p", False)]) is True
    assert server._is_unhealthy([make_pod("p", True)]) is False


def test_api_requires_token_when_configured(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "secret-token")
    client = TestClient(server.app)  # not used as context manager -> watcher not started

    assert client.get("/health").status_code == 200
    assert client.get("/incidents").status_code == 401
    assert client.get("/incidents", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/incidents",
                      headers={"Authorization": "Bearer secret-token"}).status_code == 200


def test_api_open_when_no_token(monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "")
    client = TestClient(server.app)
    assert client.get("/incidents").status_code == 200


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


def test_graph_expired_approval_does_nothing(fake_agent):
    run, executed, audit_file = fake_agent
    interrupted, state = run("rollback", answer="expired")

    assert interrupted is True
    assert state["approval"] == "expired"
    assert executed == []
    assert json.loads(audit_file.read_text().strip())["approval"] == "expired"


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
