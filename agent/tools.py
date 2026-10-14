"""Read-only Kubernetes signal tools used by the agent. All text is redacted."""

import json
from datetime import datetime, timezone

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from redact import redact_env, redact_text

REVISION_ANNOTATION = "deployment.kubernetes.io/revision"


def _load_config() -> None:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def _core_api() -> client.CoreV1Api:
    _load_config()
    return client.CoreV1Api()


def _apps_api() -> client.AppsV1Api:
    _load_config()
    return client.AppsV1Api()


def get_pod_status(namespace: str, label_selector: str) -> list[dict]:
    """Return phase, readiness, restarts and state for matching pods (skips terminating pods)."""
    v1 = _core_api()
    pods = v1.list_namespaced_pod(namespace, label_selector=label_selector)
    now = datetime.now(timezone.utc)

    result = []
    for pod in pods.items:
        # Pods being deleted (old pods during a rollout) are not the live app
        if pod.metadata.deletion_timestamp:
            continue

        containers = []
        for c in pod.status.container_statuses or []:
            state, reason, running_for = "unknown", None, None
            if c.state.running:
                state = "running"
                if c.state.running.started_at:
                    running_for = int((now - c.state.running.started_at).total_seconds())
            elif c.state.waiting:
                state, reason = "waiting", c.state.waiting.reason
            elif c.state.terminated:
                state, reason = "terminated", c.state.terminated.reason

            last_reason = None
            if c.last_state and c.last_state.terminated:
                last_reason = c.last_state.terminated.reason

            containers.append({
                "name": c.name,
                "ready": c.ready,
                "restart_count": c.restart_count,
                "state": state,
                "waiting_reason": reason if state == "waiting" else None,
                "terminated_reason": reason if state == "terminated" else None,
                "running_for_seconds": running_for,
                "last_termination_reason": last_reason,
                "image": c.image,
            })

        result.append({
            "pod": pod.metadata.name,
            "phase": pod.status.phase,
            "node": pod.spec.node_name,
            "containers": containers,
        })
    return result


def get_pod_logs(namespace: str, pod_name: str, tail_lines: int = 50,
                 previous: bool = False) -> str:
    """Return recent (redacted) logs. previous=True gives logs of the crashed container."""
    v1 = _core_api()
    try:
        logs = v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=tail_lines,
            previous=previous,
        )
        return redact_text(logs)
    except ApiException as e:
        return f"Could not read logs: {e.reason}"


def get_events(namespace: str, pod_name: str | None = None,
               limit: int = 20) -> list[dict]:
    """Return recent events, optionally filtered to one pod."""
    v1 = _core_api()
    field_selector = f"involvedObject.name={pod_name}" if pod_name else None
    events = v1.list_namespaced_event(namespace, field_selector=field_selector)

    items = sorted(
        events.items,
        key=lambda e: e.last_timestamp or e.event_time or e.metadata.creation_timestamp,
    )

    return [
        {
            "type": e.type,
            "reason": e.reason,
            "message": redact_text(e.message),
            "count": e.count,
            "last_seen": str(e.last_timestamp or e.event_time),
        }
        for e in items[-limit:]
    ]


def get_deployment_revisions(namespace: str, name: str, limit: int = 3) -> dict:
    """Return the deployment's recent revisions (image, env, age) - shows what changed."""
    apps = _apps_api()
    try:
        d = apps.read_namespaced_deployment(name, namespace)
    except ApiException as e:
        return {"error": f"Could not read deployment: {e.reason}"}

    current = int((d.metadata.annotations or {}).get(REVISION_ANNOTATION, 0))
    selector = ",".join(f"{k}={v}" for k, v in (d.spec.selector.match_labels or {}).items())
    replica_sets = apps.list_namespaced_replica_set(namespace, label_selector=selector).items
    now = datetime.now(timezone.utc)

    revisions = []
    for rs in replica_sets:
        owners = rs.metadata.owner_references or []
        if not any(o.uid == d.metadata.uid for o in owners):
            continue

        containers = rs.spec.template.spec.containers
        revision = int((rs.metadata.annotations or {}).get(REVISION_ANNOTATION, 0))
        env = {
            e.name: (e.value if e.value is not None else "<from reference>")
            for c in containers for e in (c.env or [])
        }
        revisions.append({
            "revision": revision,
            "current": revision == current,
            "age_seconds": int((now - rs.metadata.creation_timestamp).total_seconds()),
            "images": [c.image for c in containers],
            "env": redact_env(env),
            "ready_replicas": rs.status.ready_replicas or 0,
        })

    revisions.sort(key=lambda r: r["revision"], reverse=True)
    return {"current_revision": current, "revisions": revisions[:limit]}


if __name__ == "__main__":
    ns, selector, deploy = "sample-app", "app=sample-app", "sample-app"

    pods = get_pod_status(ns, selector)
    print("=== POD STATUS ===")
    print(json.dumps(pods, indent=2))

    print("\n=== DEPLOYMENT REVISIONS ===")
    print(json.dumps(get_deployment_revisions(ns, deploy), indent=2))

    if pods:
        pod_name = pods[0]["pod"]
        print("\n=== EVENTS ===")
        print(json.dumps(get_events(ns, pod_name), indent=2))

        print("\n=== CURRENT LOGS ===")
        print(get_pod_logs(ns, pod_name, tail_lines=10))

        print("\n=== PREVIOUS (CRASHED) CONTAINER LOGS ===")
        print(get_pod_logs(ns, pod_name, tail_lines=10, previous=True))
