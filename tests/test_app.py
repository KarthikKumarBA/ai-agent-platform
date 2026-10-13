import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "sample-app")
)

from app import app, service_state
from fastapi.testclient import TestClient

client = TestClient(app)


def setup_function():
    service_state["healthy"] = True


def test_home():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["message"] == "Sample application is running"


def test_health_when_healthy():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_failure_and_recovery():
    failure = client.post("/simulate-failure")
    assert failure.status_code == 200

    health = client.get("/health")
    assert health.status_code == 503
    assert health.json()["status"] == "unhealthy"

    recovery = client.post("/simulate-recovery")
    assert recovery.status_code == 200

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "healthy"


def test_status():
    response = client.get("/status")
    assert response.status_code == 200
    assert response.json()["application"] == "sample-app"
