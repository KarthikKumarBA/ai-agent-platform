"""Summarize sample-app state using any provider and model.

Usage:
  python agent/llm_summary.py                         # provider + model from .env
  python agent/llm_summary.py openai                  # provider, default model
  python agent/llm_summary.py openai gpt-4o-mini      # provider + specific model
  python agent/llm_summary.py anthropic claude-sonnet-5-5
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm import get_llm, response_text  # noqa: E402
from tools import get_events, get_pod_logs, get_pod_status  # noqa: E402

NAMESPACE = "sample-app"
SELECTOR = "app=sample-app"

SYSTEM_PROMPT = (
    "You are an SRE assistant. From the Kubernetes signals, reply in max 6 short lines: "
    "status, what happened, root cause, action. Use only the given evidence."
)


def collect_signals() -> dict:
    pods = get_pod_status(NAMESPACE, SELECTOR)
    if not pods:
        return {"pods": []}

    pod_name = pods[0]["pod"]
    return {
        "pods": pods,
        "events": get_events(NAMESPACE, pod_name, limit=10),
        "logs": get_pod_logs(NAMESPACE, pod_name, tail_lines=10),
        "previous_logs": get_pod_logs(NAMESPACE, pod_name, tail_lines=10, previous=True),
    }


def main() -> None:
    provider = sys.argv[1] if len(sys.argv) > 1 else None
    model = sys.argv[2] if len(sys.argv) > 2 else None
    llm = get_llm(provider, model)

    messages = [
        ("system", SYSTEM_PROMPT),
        ("human", json.dumps(collect_signals(), separators=(",", ":"), default=str)),
    ]

    start = time.perf_counter()
    response = llm.invoke(messages)
    elapsed = time.perf_counter() - start

    model_name = getattr(llm, "model", None) or getattr(llm, "model_id", "")
    print(f"=== {type(llm).__name__} | {model_name} ===")
    print(response_text(response))
    print("\n--- usage ---")
    print(f"latency: {elapsed:.2f}s")
    print(f"tokens:  {response.usage_metadata}")


if __name__ == "__main__":
    main()
