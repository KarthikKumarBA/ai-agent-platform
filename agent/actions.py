"""Allowlisted remediation actions. Everything defaults to dry_run=True."""

import sys
import time
from datetime import datetime, timezone

from kubernetes import client, config

ALLOWED_ACTIONS = {"restart_deployment", "scale_up", "rollback"}
MAX_REPLICAS = 3
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"


def _apps_api() -> client.AppsV1Api:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.AppsV1Api()


def _revision(obj) -> int:
    return int((obj.metadata.annotations or {}).get(REVISION_ANNOTATION, 0))


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


def rollback_deployment(namespace: str, name: str, dry_run: bool = True) -> dict:
    """Roll back to the previous revision (same as: kubectl rollout undo deployment/<name>)."""
    api = _apps_api()
    d = api.read_namespaced_deployment(name, namespace)
    current = _revision(d)

    selector = ",".join(f"{k}={v}" for k, v in (d.spec.selector.match_labels or {}).items())
    owned = [
        rs for rs in api.list_namespaced_replica_set(namespace, label_selector=selector).items
        if any(o.uid == d.metadata.uid for o in (rs.metadata.owner_references or []))
    ]
    older = sorted((rs for rs in owned if _revision(rs) < current), key=_revision)

    if not older:
        return {"action": "rollback", "executed": False,
                "reason": "No previous revision to roll back to"}

    target = older[-1]
    result = {
        "action": "rollback",
        "from_revision": current,
        "to_revision": _revision(target),
        "image": target.spec.template.spec.containers[0].image,
    }

    if dry_run:
        return {**result, "executed": False, "dry_run": True}

    # Use the previous ReplicaSet's pod template (without its generated hash label)
    template = target.spec.template
    labels = dict(template.metadata.labels or {})
    labels.pop("pod-template-hash", None)
    template.metadata.labels = labels

    d.spec.template = template
    api.replace_namespaced_deployment(name, namespace, d)
    return {**result, "executed": True, "dry_run": False}


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

    handlers = {
        "restart_deployment": restart_deployment,
        "scale_up": scale_up,
        "rollback": rollback_deployment,
    }
    return handlers[action](namespace, name, dry_run=dry_run)


if __name__ == "__main__":
    # python agent/actions.py   -> dry runs only, nothing changes
    ns, deploy = "sample-app", "sample-app"

    print(execute_action("delete_namespace", ns, deploy))  # blocked by allowlist
    print(execute_action("restart_deployment", ns, deploy, dry_run=True))
    print(execute_action("rollback", ns, deploy, dry_run=True))
