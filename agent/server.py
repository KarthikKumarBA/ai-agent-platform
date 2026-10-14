"""Phase 9: agent as an HTTP service with approval endpoints + automatic watcher.

Run locally:
  python -m uvicorn server:app --app-dir agent --port 8090

Auth: set AGENT_API_TOKEN and send header  Authorization: Bearer <token>
"""

import logging
import os
import secrets
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
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
UNHEALTHY_THRESHOLD = int(os.getenv("UNHEALTHY_THRESHOLD_SECONDS", "30"))
COOLDOWN = int(os.getenv("INCIDENT_COOLDOWN_SECONDS", "120"))
# Short cooldown when the agent decided "none" - re-check soon in case it was too early
NONE_COOLDOWN = int(os.getenv("NONE_COOLDOWN_SECONDS", "30"))
# Pending approvals older than this are expired so they can't block the watcher forever
APPROVAL_TIMEOUT = int(os.getenv("APPROVAL_TIMEOUT_SECONDS", "600"))

API_TOKEN = os.getenv("AGENT_API_TOKEN", "")

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


# ---------- Auth ----------
def require_token(authorization: str | None = Header(default=None)) -> None:
    """Bearer-token check. Disabled only when AGENT_API_TOKEN is not set."""
    if not API_TOKEN:
        return
    expected = f"Bearer {API_TOKEN}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(401, "Missing or invalid API token")


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
    info["action"] = (values.get("diagnosis") or {}).get("recommended_action")
    info["approval"] = values.get("approval")
    if info["status"] == "completed" and "completed_at" not in info:
        info["completed_at"] = time.time()

    return {
        "id": incident_id,
        "status": info["status"],
        "source": info["source"],
        "created": info["created"],
        "pending_approval": pending,
        "approval_expires_in_seconds": (
            max(0, int(APPROVAL_TIMEOUT - (time.time() - info["created_ts"]))) if pending else None
        ),
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
        "created_ts": time.time(),
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


def _expire_stale_approvals() -> None:
    """Close pending incidents nobody answered within APPROVAL_TIMEOUT. Call while holding _lock."""
    now = time.time()
    for incident_id, info in list(INCIDENTS.items()):
        if info["status"] == "pending_approval" and now - info["created_ts"] > APPROVAL_TIMEOUT:
            logger.warning("Incident %s approval expired after %ss", incident_id, APPROVAL_TIMEOUT)
            graph.invoke(Command(resume="expired"), _config(incident_id))
            _snapshot(incident_id)


def _open_incident_exists() -> bool:
    return any(i["status"] in ("running", "pending_approval") for i in INCIDENTS.values())


def _in_cooldown() -> bool:
    now = time.time()
    for info in INCIDENTS.values():
        if "completed_at" not in info:
            continue
        window = NONE_COOLDOWN if info.get("action") == "none" else COOLDOWN
        if now - info["completed_at"] < window:
            return True
    return False


# ---------- Watcher ----------
def _is_unhealthy(pods: list[dict]) -> bool:
    if not pods:
        return True
    return any(not c["ready"] for p in pods for c in p["containers"]) or any(
        not p["containers"] for p in pods
    )


def watch_loop() -> None:
    logger.info("Watcher started: %s/%s every %ss (threshold %ss, cooldown %ss, "
                "none-cooldown %ss, approval-timeout %ss)",
                TARGET_NAMESPACE, TARGET_DEPLOYMENT, WATCH_INTERVAL, UNHEALTHY_THRESHOLD,
                COOLDOWN, NONE_COOLDOWN, APPROVAL_TIMEOUT)
    unhealthy_since = None

    while True:
        try:
            with _lock:
                _expire_stale_approvals()

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
    if not API_TOKEN:
        logger.warning("AGENT_API_TOKEN is not set - API authentication is DISABLED")
    if WATCH_ENABLED:
        threading.Thread(target=watch_loop, daemon=True, name="watcher").start()
    yield


app = FastAPI(title="ai-agent", lifespan=lifespan)
protected = APIRouter(dependencies=[Depends(require_token)])


# ---------- API ----------
@app.get("/health")
def health() -> dict:
    return {"status": "healthy"}


@protected.post("/incidents")
def create_incident(req: IncidentRequest | None = None) -> dict:
    with _lock:
        _expire_stale_approvals()
        return _start_incident(req or IncidentRequest(), source="manual")


@protected.post("/alerts")
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


@protected.get("/incidents")
def list_incidents() -> list[dict]:
    with _lock:
        _expire_stale_approvals()
        return [
            {"id": i, "status": info["status"], "source": info["source"],
             "action": info.get("action"), "approval": info.get("approval"),
             "created": info["created"]}
            for i, info in INCIDENTS.items()
        ]


@protected.get("/incidents/{incident_id}")
def get_incident(incident_id: str) -> dict:
    with _lock:
        _expire_stale_approvals()
        if incident_id not in INCIDENTS:
            raise HTTPException(404, "Incident not found")
        return _snapshot(incident_id)


@protected.post("/incidents/{incident_id}/decision")
def decide(incident_id: str, decision: Decision) -> dict:
    with _lock:
        _expire_stale_approvals()
        if incident_id not in INCIDENTS:
            raise HTTPException(404, "Incident not found")
        if INCIDENTS[incident_id]["status"] != "pending_approval":
            raise HTTPException(409, "Incident is not waiting for approval")

        graph.invoke(Command(resume="y" if decision.approve else "n"), _config(incident_id))
        return _snapshot(incident_id)


app.include_router(protected)
