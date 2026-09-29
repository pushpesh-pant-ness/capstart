"""
Read-only cluster snapshot used by the dashboard - pods/nodes across the
namespaces this demo cares about. Nothing here mutates cluster state; it is
purely so you can watch an injected fault (and its remediation) happen live
in the UI instead of switching to a terminal.
"""
from __future__ import annotations

from typing import Any

from .kube_client import core_v1

NAMESPACES = ["demo", "monitoring", "agent"]


def _pod_summary(pod: Any) -> dict[str, Any]:
    statuses = pod.status.container_statuses or []
    restarts = sum(s.restart_count for s in statuses)
    ready_count = sum(1 for s in statuses if s.ready)
    waiting_reason = None
    for s in statuses:
        if s.state and s.state.waiting:
            waiting_reason = s.state.waiting.reason
            break
    return {
        "namespace": pod.metadata.namespace,
        "name": pod.metadata.name,
        "phase": pod.status.phase,
        "ready": f"{ready_count}/{len(statuses)}" if statuses else "0/0",
        "restarts": restarts,
        "node": pod.spec.node_name,
        "status_text": waiting_reason or pod.status.phase,
        "healthy": waiting_reason is None and pod.status.phase == "Running" and ready_count == len(statuses),
    }


def get_pods() -> list[dict[str, Any]]:
    core = core_v1()
    pods: list[dict[str, Any]] = []
    for ns in NAMESPACES:
        result = core.list_namespaced_pod(ns)
        pods.extend(_pod_summary(p) for p in result.items)
    return pods


def get_nodes() -> list[dict[str, Any]]:
    core = core_v1()
    out: list[dict[str, Any]] = []
    for n in core.list_node().items:
        conditions = {c.type: c.status for c in (n.status.conditions or [])}
        out.append(
            {
                "name": n.metadata.name,
                "ready": conditions.get("Ready") == "True",
                "unschedulable": bool(n.spec.unschedulable),
            }
        )
    return out
