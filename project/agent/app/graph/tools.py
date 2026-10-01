"""
Read-only diagnostic toolbelt for the agentic investigate node (Phase 1).

Every function here only READS cluster/metrics/log state - there are no
mutating verbs in this module at all. That is the safety boundary for giving
the LLM free rein to investigate: it may call any of these in any order to
gather evidence, but it can never change the cluster from here. Actuation
still only happens post-approval in executor.py, via the deterministic
allow-listed action set (remediation/templates.py + guardrail.py).

Each tool returns a compact JSON-serialisable dict (trimmed, not raw client
objects) so it fits cheaply into the model's context, and never raises -
failures come back as {"error": ...} so one bad call can't crash the loop.
"""
from __future__ import annotations

from typing import Any

from . import heuristics  # noqa: F401  (kept for parity; scoring lives there)
from ..diagnosis.context import query_loki, query_prometheus
from ..kube_client import apps_v1, core_v1, networking_v1


def prometheus_query(promql: str, incident_id: int | None = None) -> dict[str, Any]:
    """Run an instant PromQL query and return Prometheus' JSON response."""
    return query_prometheus(incident_id, promql)


def loki_logs(namespace: str, pod: str, incident_id: int | None = None, limit: int = 20) -> dict[str, Any]:
    """Recent log lines for a pod (LogQL {namespace,pod}) from Loki."""
    return query_loki(incident_id, f'{{namespace="{namespace}", pod="{pod}"}}', limit=limit)


def get_pod(namespace: str, name: str) -> dict[str, Any]:
    """Pod phase, per-container ready/restart counts, and any waiting reason."""
    try:
        pod = core_v1().read_namespaced_pod(name=name, namespace=namespace)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    statuses = pod.status.container_statuses or []
    containers = []
    for s in statuses:
        waiting = s.state.waiting.reason if s.state and s.state.waiting else None
        terminated = s.state.terminated.reason if s.state and s.state.terminated else None
        containers.append(
            {
                "name": s.name,
                "ready": s.ready,
                "restart_count": s.restart_count,
                "waiting_reason": waiting,
                "terminated_reason": terminated,
            }
        )
    return {
        "namespace": pod.metadata.namespace,
        "name": pod.metadata.name,
        "phase": pod.status.phase,
        "node": pod.spec.node_name,
        "containers": containers,
    }


def describe_deployment(namespace: str, name: str) -> dict[str, Any]:
    """Deployment spec vs available replicas, container images, and conditions."""
    try:
        dep = apps_v1().read_namespaced_deployment(name=name, namespace=namespace)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    containers = [
        {"name": c.name, "image": c.image}
        for c in (dep.spec.template.spec.containers or [])
    ]
    conditions = [
        {"type": c.type, "status": c.status, "reason": c.reason}
        for c in (dep.status.conditions or [])
    ]
    return {
        "namespace": dep.metadata.namespace,
        "name": dep.metadata.name,
        "spec_replicas": dep.spec.replicas,
        "available_replicas": dep.status.available_replicas,
        "unavailable_replicas": dep.status.unavailable_replicas,
        "containers": containers,
        "conditions": conditions,
    }


def list_events(namespace: str, name: str, limit: int = 15) -> dict[str, Any]:
    """Recent Kubernetes events for one object (Warning events first)."""
    try:
        events = core_v1().list_namespaced_event(
            namespace=namespace, field_selector=f"involvedObject.name={name}"
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    items = sorted(
        events.items,
        key=lambda e: (e.type != "Warning", e.last_timestamp or e.event_time or ""),
    )
    return {
        "namespace": namespace,
        "name": name,
        "events": [
            {"type": e.type, "reason": e.reason, "message": e.message, "count": e.count}
            for e in items[:limit]
        ],
    }


def get_node(name: str) -> dict[str, Any]:
    """Node Ready condition, schedulability, and all status conditions."""
    try:
        node = core_v1().read_node(name=name)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    conditions = {c.type: c.status for c in (node.status.conditions or [])}
    return {
        "name": node.metadata.name,
        "ready": conditions.get("Ready") == "True",
        "unschedulable": bool(node.spec.unschedulable),
        "conditions": conditions,
    }


def get_endpoints(namespace: str, name: str) -> dict[str, Any]:
    """Backing addresses of a Service's Endpoints object (ready vs not-ready)."""
    try:
        ep = core_v1().read_namespaced_endpoints(name=name, namespace=namespace)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    ready, not_ready = [], []
    for subset in ep.subsets or []:
        ready.extend(a.ip for a in (subset.addresses or []))
        not_ready.extend(a.ip for a in (subset.not_ready_addresses or []))
    return {
        "namespace": namespace,
        "name": name,
        "ready_addresses": ready,
        "not_ready_addresses": not_ready,
        "ready_count": len(ready),
    }


def list_networkpolicies(namespace: str) -> dict[str, Any]:
    """NetworkPolicies in a namespace (name, pod selector, policy types)."""
    try:
        policies = networking_v1().list_namespaced_network_policy(namespace=namespace)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    return {
        "namespace": namespace,
        "policies": [
            {
                "name": p.metadata.name,
                "labels": p.metadata.labels or {},
                "pod_selector": (p.spec.pod_selector.match_labels or {}) if p.spec.pod_selector else {},
                "policy_types": p.spec.policy_types or [],
            }
            for p in policies.items
        ],
    }


# The tools an investigating agent may call, keyed by name. Phase 1 wraps these
# with LangChain @tool bindings (target namespace/resource bound server-side).
READ_ONLY_TOOLS = {
    "prometheus_query": prometheus_query,
    "loki_logs": loki_logs,
    "get_pod": get_pod,
    "describe_deployment": describe_deployment,
    "list_events": list_events,
    "get_node": get_node,
    "get_endpoints": get_endpoints,
    "list_networkpolicies": list_networkpolicies,
}
