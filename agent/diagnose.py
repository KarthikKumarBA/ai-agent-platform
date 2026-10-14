"""Phase 7: read-only LangGraph diagnosis agent.

Usage:
  python agent/diagnose.py                         # provider + model from .env
  python agent/diagnose.py anthropic
  python agent/diagnose.py openai gpt-5.6-luna
"""

import json
import sys
from pathlib import Path
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm import get_llm  # noqa: E402
from tools import get_events, get_pod_logs, get_pod_status  # noqa: E402


# ---------- Structured output schema ----------
class Diagnosis(BaseModel):
    status: Literal["healthy", "degraded", "down", "unknown"]
    summary: str = Field(description="One-line summary of the situation")
    evidence: list[str] = Field(description="Key facts from the signals, max 4")
    root_cause: str = Field(description="Most likely root cause based only on evidence")
    confidence: Literal["low", "medium", "high"]
    recommended_action: Literal[
        "none", "restart_deployment", "scale_up", "rollback", "investigate"
    ]
    reason: str = Field(description="Why this action is recommended")


# ---------- Graph state ----------
class AgentState(TypedDict, total=False):
    namespace: str
    selector: str
    provider: str | None
    model: str | None
    signals: dict
    diagnosis: dict
    usage: dict


SYSTEM_PROMPT = (
    "You are an SRE diagnosis agent for Kubernetes. Analyze the signals and fill the "
    "diagnosis schema. Use only the given evidence. Pick recommended_action from the "
    "allowed values. If the app is healthy now, say so, and note any past incident."
)


# ---------- Helpers ----------
def _pick_focus_pod(pods: list[dict]) -> dict:
    """Prefer the first unhealthy pod; fall back to the first pod."""
    for pod in pods:
        if not pod["containers"] or any(not c["ready"] for c in pod["containers"]):
            return pod
    return pods[0]


# ---------- Nodes ----------
def collect_node(state: AgentState) -> AgentState:
    ns, selector = state["namespace"], state["selector"]
    pods = get_pod_status(ns, selector)

    if not pods:
        return {"signals": {"pods": []}}

    focus = _pick_focus_pod(pods)
    pod_name = focus["pod"]
    return {
        "signals": {
            "pods": pods,
            "focus_pod": pod_name,
            "events": get_events(ns, pod_name, limit=10),
            "logs": get_pod_logs(ns, pod_name, tail_lines=10),
            "previous_logs": get_pod_logs(ns, pod_name, tail_lines=10, previous=True),
        }
    }


def route_after_collect(state: AgentState) -> str:
    return "analyze" if state["signals"].get("pods") else "no_pods"


def no_pods_node(state: AgentState) -> AgentState:
    diagnosis = Diagnosis(
        status="down",
        summary="No pods found for the application",
        evidence=[f"No pods match '{state['selector']}' in namespace '{state['namespace']}'"],
        root_cause="Deployment missing, scaled to 0, or wrong label selector",
        confidence="high",
        recommended_action="investigate",
        reason="Nothing is running, so there is nothing to restart",
    )
    return {"diagnosis": diagnosis.model_dump(), "usage": {}}


def analyze_node(state: AgentState) -> AgentState:
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


def report_node(state: AgentState) -> AgentState:
    d = state["diagnosis"]
    print("\n=== DIAGNOSIS ===")
    print(f"Status:      {d['status']}")
    print(f"Summary:     {d['summary']}")
    print("Evidence:")
    for item in d["evidence"]:
        print(f"  - {item}")
    print(f"Root cause:  {d['root_cause']}")
    print(f"Confidence:  {d['confidence']}")
    print(f"Action:      {d['recommended_action']}")
    print(f"Reason:      {d['reason']}")
    if state.get("usage"):
        print(f"\nTokens:      {state['usage']}")
    return {}


# ---------- Graph ----------
def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("collect", collect_node)
    graph.add_node("analyze", analyze_node)
    graph.add_node("no_pods", no_pods_node)
    graph.add_node("report", report_node)

    graph.add_edge(START, "collect")
    graph.add_conditional_edges(
        "collect", route_after_collect, {"analyze": "analyze", "no_pods": "no_pods"}
    )
    graph.add_edge("analyze", "report")
    graph.add_edge("no_pods", "report")
    graph.add_edge("report", END)

    return graph.compile()


def main() -> None:
    provider = sys.argv[1] if len(sys.argv) > 1 else None
    model = sys.argv[2] if len(sys.argv) > 2 else None

    app = build_graph()
    app.invoke({
        "namespace": "sample-app",
        "selector": "app=sample-app",
        "provider": provider,
        "model": model,
    })


if __name__ == "__main__":
    main()
