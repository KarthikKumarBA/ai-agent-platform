"""Allowlisted remediation actions. Everything defaults to dry_run=True."""

import sys
import time
from datetime import datetime, timezone

from kubernetes import client, config

ALLOWED_ACTIONS = {"restart_deployment", "scale_up"}
MAX_REPLICAS = 3


def _apps_api() -> client.AppsV1Api:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.AppsV1Api()


def restart_deployment(namespace: str, name: str, dry_run: bool = True) -> dict:
    """Rolling restart (same as: kubectl rollout restart deployment/<name>)."""
    if dry_run:
        return {"action": "restart_deployment", "executed": False, "dry_run": True}

    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": datetime.now(timezone.utc).isoformat()
                    }
                }
            }
        }
    }
    _apps_api().patch_namespaced_deployment(name, namespace, patch)
    return {"action": "restart_deployment", "executed": True, "dry_run": False}


def scale_up(namespace: str, name: str, dry_run: bool = True) -> dict:
    """Add one replica, capped at MAX_REPLICAS."""
    api = _apps_api()
    current = api.read_namespaced_deployment(name, namespace).spec.replicas or 0
    target = current + 1

    if target > MAX_REPLICAS:
        return {"action": "scale_up", "executed": False,
                "reason": f"Already at max replicas ({MAX_REPLICAS})"}

    if dry_run:
        return {"action": "scale_up", "executed": False, "dry_run": True,
                "from": current, "to": target}

    api.patch_namespaced_deployment_scale(name, namespace, {"spec": {"replicas": target}})
    return {"action": "scale_up", "executed": True, "dry_run": False,
            "from": current, "to": target}


def wait_for_healthy(namespace: str, name: str, timeout: int = 120) -> dict:
    """Poll until all replicas are updated and ready, or timeout."""
    api = _apps_api()
    start = time.time()

    while time.time() - start < timeout:
        d = api.read_namespaced_deployment(name, namespace)
        desired = d.spec.replicas or 0
        s = d.status
        if (
            (s.observed_generation or 0) >= (d.metadata.generation or 0)
            and (s.updated_replicas or 0) == desired
            and (s.ready_replicas or 0) == desired
            and (s.unavailable_replicas or 0) == 0
        ):
            return {"healthy": True, "seconds": round(time.time() - start, 1),
                    "ready": f"{s.ready_replicas}/{desired}"}
        time.sleep(3)

    return {"healthy": False, "seconds": timeout,
            "ready": f"{d.status.ready_replicas or 0}/{d.spec.replicas}"}


def execute_action(action: str, namespace: str, name: str, dry_run: bool = True) -> dict:
    """Single entry point: refuses anything outside the allowlist."""
    if action not in ALLOWED_ACTIONS:
        return {"action": action, "executed": False,
                "reason": f"'{action}' is not in the allowlist {sorted(ALLOWED_ACTIONS)}"}

    handlers = {"restart_deployment": restart_deployment, "scale_up": scale_up}
    return handlers[action](namespace, name, dry_run=dry_run)


if __name__ == "__main__":
    # python agent/actions.py           -> dry run
    # python agent/actions.py --apply   -> real restart + verify
    apply = "--apply" in sys.argv
    ns, deploy = "sample-app", "sample-app"

    print(execute_action("rollback", ns, deploy))  # blocked by allowlist
    result = execute_action("restart_deployment", ns, deploy, dry_run=not apply)
    print(result)

    if result.get("executed"):
        print("Verifying...")
        print(wait_for_healthy(ns, deploy))
