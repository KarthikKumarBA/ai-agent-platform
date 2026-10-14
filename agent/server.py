"""Phase 9: agent as an HTTP service with approval endpoints + automatic watcher.

Run locally:
  python -m uvicorn server:app --app-dir agent --port 8090
"""

import logging
import os
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from langgraph.types import Command
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
from remediate import build_graph  # noqa: E402
from tools import get_pod_status  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ai-agent")

TARGET_NAMESPACE = os.getenv("TARGET_NAMESPACE", "sample-app")
TARGET_DEPLOYMENT = os.getenv("TARGET_DEPLOYMENT", "sample-app")
TARGET_SELECTOR = os.getenv("TARGET_SELECTOR", "app=sample-app")
DRY_RUN = os.getenv("AGENT_DRY_RUN", "false").lower() == "true"

WATCH_ENABLED = os.getenv("WATCH_ENABLED", "true").lower() == "true"
WATCH_INTERVAL = int(os.getenv("WATCH_INTERVAL_SECONDS", "10"))
UNHEALTHY_THRESHOLD = int(os.getenv("UNHEALTHY_THRESHOLD_SECONDS", "10"))
COOLDOWN = int(os.getenv("INCIDENT_COOLDOWN_SECONDS", "120"))

graph = build_graph()
INCIDENTS: dict[str, dict] = {}
_lock = threading.Lock()


class IncidentRequest(BaseModel):
    namespace: str | None = None
    deployment: str | None = None
    selector: str | None = None
    provider: str | None = None
    model: str | None = None


class Decision(BaseModel):
    approve: bool


# ---------- Incident helpers ----------
def _config(incident_id: str) -> dict:
    return {"configurable": {"thread_id": incident_id}}


def _snapshot(incident_id: str) -> dict:
    snap = graph.get_state(_config(incident_id))
    values = snap.values
    info = INCIDENTS[incident_id]

    pending = None
    if snap.next and snap.tasks and snap.tasks[0].interrupts:
        pending = snap.tasks[0].interrupts[0].value

    info["status"] = "pending_approval" if pending else "completed"
    if info["status"] == "completed" and "completed_at" not in info:
        info["completed_at"] = time.time()

    return {
        "id": incident_id,
        "status": info["status"],
        "source": info["source"],
        "created": info["created"],
        "pending_approval": pending,
        "diagnosis": values.get("diagnosis"),
        "approval": values.get("approval"),
        "action_result": values.get("action_result"),
        "verification": values.get("verification"),
        "tokens": (values.get("usage") or {}).get("total_tokens"),
    }


def _start_incident(req: IncidentRequest, source: str) -> dict:
    incident_id = str(uuid.uuid4())[:8]
    INCIDENTS[incident_id] = {
        "created": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "source": source,
    }
    graph.invoke({
        "namespace": req.namespace or TARGET_NAMESPACE,
        "deployment": req.deployment or TARGET_DEPLOYMENT,
        "selector": req.selector or TARGET_SELECTOR,
        "provider": req.provider,
        "model": req.model,
        "dry_run": DRY_RUN,
    }, _config(incident_id))
    return _snapshot(incident_id)


def _open_incident_exists() -> bool:
    return any(i["status"] in ("running", "pending_approval") for i in INCIDENTS.values())


def _in_cooldown() -> bool:
    finished = [i["completed_at"] for i in INCIDENTS.values() if "completed_at" in i]
    return bool(finished) and time.time() - max(finished) < COOLDOWN


# ---------- Watcher ----------
def _is_unhealthy(pods: list[dict]) -> bool:
    if not pods:
        return True
    return any(not c["ready"] for p in pods for c in p["containers"]) or any(
        not p["containers"] for p in pods
    )


def watch_loop() -> None:
    logger.info("Watcher started: %s/%s every %ss (threshold %ss, cooldown %ss)",
                TARGET_NAMESPACE, TARGET_DEPLOYMENT, WATCH_INTERVAL, UNHEALTHY_THRESHOLD, COOLDOWN)
    unhealthy_since = None

    while True:
        try:
            pods = get_pod_status(TARGET_NAMESPACE, TARGET_SELECTOR)
            if _is_unhealthy(pods):
                unhealthy_since = unhealthy_since or time.time()
                duration = time.time() - unhealthy_since

                if duration >= UNHEALTHY_THRESHOLD:
                    with _lock:
                        if not _open_incident_exists() and not _in_cooldown():
                            logger.warning("App unhealthy for %.0fs - creating incident", duration)
                            result = _start_incident(IncidentRequest(), source="watcher")
                            logger.warning("Incident %s: status=%s action=%s",
                                           result["id"], result["status"],
                                           (result["diagnosis"] or {}).get("recommended_action"))
                            unhealthy_since = None
            else:
                unhealthy_since = None
        except Exception:
            logger.exception("Watcher error")

        time.sleep(WATCH_INTERVAL)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if WATCH_ENABLED:
        threading.Thread(target=watch_loop, daemon=True, name="watcher").start()
    yield


app = FastAPI(title="ai-agent", lifespan=lifespan)


# ---------- API ----------
@app.get("/health")
def health() -> dict:
    return {"status": "healthy"}


@app.post("/incidents")
def create_incident(req: IncidentRequest | None = None) -> dict:
    with _lock:
        return _start_incident(req or IncidentRequest(), source="manual")


@app.post("/alerts")
def alertmanager_webhook(payload: dict) -> dict:
    """Prometheus Alertmanager webhook: one incident per firing alert."""
    created = []
    for alert in payload.get("alerts", []):
        if alert.get("status") != "firing":
            continue
        labels = alert.get("labels", {})
        req = IncidentRequest(
            namespace=labels.get("namespace"),
            deployment=labels.get("deployment"),
        )
        with _lock:
            created.append(_start_incident(req, source="alertmanager")["id"])
    return {"created": created}


@app.get("/incidents")
def list_incidents() -> list[dict]:
    return [
        {"id": i, "status": info["status"], "source": info["source"], "created": info["created"]}
        for i, info in INCIDENTS.items()
    ]


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str) -> dict:
    if incident_id not in INCIDENTS:
        raise HTTPException(404, "Incident not found")
    return _snapshot(incident_id)


@app.post("/incidents/{incident_id}/decision")
def decide(incident_id: str, decision: Decision) -> dict:
    if incident_id not in INCIDENTS:
        raise HTTPException(404, "Incident not found")
    if INCIDENTS[incident_id]["status"] != "pending_approval":
        raise HTTPException(409, "Incident is not waiting for approval")

    graph.invoke(Command(resume="y" if decision.approve else "n"), _config(incident_id))
    return _snapshot(incident_id)
