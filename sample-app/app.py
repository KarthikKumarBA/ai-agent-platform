"""Sample application with health checks and failure simulations."""

import logging
import os
import sys
import threading
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sample-app")

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

# Scenario: bad configuration -> app crashes on startup -> CrashLoopBackOff
if os.getenv("CRASH_ON_STARTUP", "false").lower() == "true":
    logger.error("Startup failed: invalid configuration (CRASH_ON_STARTUP=true)")
    sys.exit(1)

app = FastAPI(title="sample-app")

service_state = {"healthy": True}
_leaked_memory: list[bytearray] = []
_leak_started = threading.Event()


@app.get("/")
def home():
    return {"message": "Sample application is running"}


@app.get("/health")
def health():
    if service_state["healthy"]:
        logger.info("Health check passed")
        return {"status": "healthy"}

    logger.error("Health check failed: service is unhealthy")
    return JSONResponse(status_code=503, content={"status": "unhealthy"})


@app.get("/status")
def status():
    return {
        "application": "sample-app",
        "healthy": service_state["healthy"],
        "environment": ENVIRONMENT,
    }


@app.post("/simulate-failure")
def simulate_failure():
    service_state["healthy"] = False
    logger.error("Simulated failure: service marked unhealthy")
    return {"message": "Failure simulated", "healthy": False}


@app.post("/simulate-recovery")
def simulate_recovery():
    service_state["healthy"] = True
    logger.info("Simulated recovery: service marked healthy")
    return {"message": "Recovery simulated", "healthy": True}


def _leak_memory() -> None:
    logger.warning("Memory leak started")
    while True:
        _leaked_memory.append(bytearray(b"x" * 10 * 1024 * 1024))  # 10 MB, really touched
        logger.warning("Memory usage growing: %d MB allocated", len(_leaked_memory) * 10)
        time.sleep(0.5)


@app.post("/simulate-memory-leak")
def simulate_memory_leak():
    # Scenario: memory leak -> container exceeds limit -> OOMKilled
    if not _leak_started.is_set():
        _leak_started.set()
        threading.Thread(target=_leak_memory, daemon=True).start()
    return {"message": "Memory leak started"}
