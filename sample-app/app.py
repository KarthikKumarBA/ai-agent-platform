from fastapi import FastAPI, Response
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sample-app")

app = FastAPI(
    title="AI Agent Platform - Demo Service",
    description="A sample application for practicing DevOps troubleshooting",
    version="1.0.0",
)

service_state = {"healthy": True}


@app.get("/")
def home():
    logger.info("Home endpoint accessed")
    return {"message": "Sample application is running"}


@app.get("/health")
def health(response: Response):
    if not service_state["healthy"]:
        response.status_code = 503
        logger.error("Health check failed: service is unhealthy")
        return {"status": "unhealthy"}

    logger.info("Health check passed")
    return {"status": "healthy"}


@app.get("/status")
def status():
    return {
        "application": "sample-app",
        "healthy": service_state["healthy"],
        "environment": "development",
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
