"""Phase 8: diagnose -> human approval -> execute -> verify -> audit.

Usage:
  python agent/remediate.py                      # provider + model from .env
  python agent/remediate.py anthropic
  python agent/remediate.py openai gpt-5.6-luna
  python agent/remediate.py --dry-run            # approve but don't change the cluster
"""

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

sys.path.insert(0, str(Path(__file__).resolve().parent))
from actions import ALLOWED_ACTIONS, execute_action, wait_for_healthy  # noqa: E402
from diagnose import Diagnosis, collect_node, no_pods_node, route_after_collect  # noqa: E402
from llm import get_llm  # noqa: E402
from tools import get_pod_status  # noqa: E402

AUDIT_FILE = Path(__file__).resolve().parents[1] / "audit" / "audit.jsonl"

SYSTEM_PROMPT = (
    "You are an SRE remediation agent for Kubernetes. Analyze the signals and fill the "
    "diagnosis schema using only the given evidence.\n"
    "Choose recommended_action using these rules IN ORDER; the first rule that matches wins:\n"
    "1. 'none' - the app is healthy now (all containers ready).\n"
    "2. 'investigate' - last_termination_reason is OOMKilled, or logs show memory growing. "
    "A restart only hides a memory leak; the code or memory limit must be fixed.\n"
    "3. 'investigate' - waiting_reason is CrashLoopBackOff, Error, ImagePullBackOff or "
    "ErrImagePull, or logs show a startup/configuration failure. A restart will not fix it.\n"
    "4. 'none' - the container restarted recently (running_for_seconds < 60) and is "
    "still starting; Kubernetes is already recovering it. Say so in the reason.\n"
    "5. 'restart_deployment' - the container is running but not ready and probes are "
    "failing, with no sign of the causes above.\n"
    "6. 'scale_up' - only if evidence shows overload (high latency, CPU saturation).\n"
    "7. 'investigate' - anything else.\n"
    "In 'reason', name the rule number you applied."
)


class RemediationState(TypedDict, total=False):
    namespace: str
    selector: str
    deployment: str
    provider: str | None
    model: str | None
    dry_run: bool
    signals: dict
    diagnosis: dict
    usage: dict
    approval: str
    action_result: dict
    verification: dict


# ---------- Nodes ----------
def analyze_node(state: RemediationState) -> RemediationState:
    llm = get_llm(state.get("provider"), state.get("model"), max_tokens=600)
    structured_llm = llm.with_structured_output(Diagnosis, include_raw=True)

    result = structured_llm.invoke([
        ("system", SYSTEM_PROMPT),
        ("human", json.dumps(state["signals"], separators=(",", ":"), default=str)),
    ])
    if result["parsing_error"]:
        raise RuntimeError(f"Could not parse diagnosis: {result['parsing_error']}")

    return {
        "diagnosis": result["parsed"].model_dump(),
        "usage": result["raw"].usage_metadata or {},
    }


def route_after_analyze(state: RemediationState) -> str:
    action = state["diagnosis"]["recommended_action"]
    return "approve" if action in ALLOWED_ACTIONS else "report"


def approve_node(state: RemediationState) -> RemediationState:
    d = state["diagnosis"]
    answer = interrupt({
        "action": d["recommended_action"],
        "deployment": f"{state['namespace']}/{state['deployment']}",
        "summary": d["summary"],
        "reason": d["reason"],
        "confidence": d["confidence"],
        "dry_run": state.get("dry_run", False),
    })
    approved = str(answer).strip().lower() in ("y", "yes")
    return {"approval": "approved" if approved else "rejected"}


def route_after_approve(state: RemediationState) -> str:
    return "execute" if state["approval"] == "approved" else "report"


def execute_node(state: RemediationState) -> RemediationState:
    result = execute_action(
        state["diagnosis"]["recommended_action"],
        state["namespace"],
        state["deployment"],
        dry_run=state.get("dry_run", False),
    )
    return {"action_result": result}


def route_after_execute(state: RemediationState) -> str:
    return "verify" if state["action_result"].get("executed") else "report"


def verify_node(state: RemediationState) -> RemediationState:
    result = wait_for_healthy(state["namespace"], state["deployment"])
    pods = get_pod_status(state["namespace"], state["selector"])
    return {"verification": {**result, "pods": [p["pod"] for p in pods]}}


def report_node(state: RemediationState) -> RemediationState:
    d = state["diagnosis"]
    print("\n=== RESULT ===")
    print(f"Status:      {d['status']}")
    print(f"Summary:     {d['summary']}")
    print(f"Root cause:  {d['root_cause']}")
    print(f"Action:      {d['recommended_action']} (confidence: {d['confidence']})")
    print(f"Reason:      {d['reason']}")
    print(f"Approval:    {state.get('approval', 'not required')}")
    if state.get("action_result"):
        print(f"Executed:    {state['action_result']}")
    if state.get("verification"):
        print(f"Verified:    {state['verification']}")
    if state.get("usage"):
        print(f"Tokens:      {state['usage'].get('total_tokens')}")

    AUDIT_FILE.parent.mkdir(exist_ok=True)
    record = {
        "time": datetime.now(timezone.utc).isoformat(),
        "deployment": f"{state['namespace']}/{state['deployment']}",
        "diagnosis": d,
        "approval": state.get("approval", "not required"),
        "action_result": state.get("action_result"),
        "verification": state.get("verification"),
        "tokens": state.get("usage", {}).get("total_tokens"),
    }
    with AUDIT_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")
    print(f"\nAudit:       {AUDIT_FILE}")
    return {}


# ---------- Graph ----------
def build_graph():
    graph = StateGraph(RemediationState)

    graph.add_node("collect", collect_node)
    graph.add_node("no_pods", no_pods_node)
    graph.add_node("analyze", analyze_node)
    graph.add_node("approve", approve_node)
    graph.add_node("execute", execute_node)
    graph.add_node("verify", verify_node)
    graph.add_node("report", report_node)

    graph.add_edge(START, "collect")
    graph.add_conditional_edges("collect", route_after_collect,
                                {"analyze": "analyze", "no_pods": "no_pods"})
    graph.add_edge("no_pods", "report")
    graph.add_conditional_edges("analyze", route_after_analyze,
                                {"approve": "approve", "report": "report"})
    graph.add_conditional_edges("approve", route_after_approve,
                                {"execute": "execute", "report": "report"})
    graph.add_conditional_edges("execute", route_after_execute,
                                {"verify": "verify", "report": "report"})
    graph.add_edge("verify", "report")
    graph.add_edge("report", END)

    # Checkpointer is required so the graph can pause (interrupt) and resume
    return graph.compile(checkpointer=InMemorySaver())


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    provider = args[0] if len(args) > 0 else None
    model = args[1] if len(args) > 1 else None

    app = build_graph()
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    app.invoke({
        "namespace": "sample-app",
        "selector": "app=sample-app",
        "deployment": "sample-app",
        "provider": provider,
        "model": model,
        "dry_run": dry_run,
    }, config)

    # If the graph paused for approval, ask the human and resume
    snapshot = app.get_state(config)
    while snapshot.next:
        request = snapshot.tasks[0].interrupts[0].value
        print("\n=== APPROVAL REQUIRED ===")
        for key, value in request.items():
            print(f"{key:<11} {value}")
        answer = input("\nApprove this action? [y/N]: ")
        app.invoke(Command(resume=answer), config)
        snapshot = app.get_state(config)


if __name__ == "__main__":
    main()
