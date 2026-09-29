"""
Rule engine step 1: turn raw Alertmanager alert labels into a structured
incident classification. This is deterministic label matching, not an LLM -
the alert rules in monitoring/prometheus already stamp an `incident_type`
label, so classification here is just extraction + a bit of normalization.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ClassifiedAlert:
    alertname: str
    incident_type: str
    namespace: str
    resource_name: str
    severity: str
    labels: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)


def _resource_name_for(incident_type: str, labels: dict[str, Any]) -> str:
    if incident_type == "crashloop":
        return labels.get("pod", labels.get("container", "unknown-pod"))
    if incident_type == "node_not_ready":
        return labels.get("node", "unknown-node")
    if incident_type == "service_unreachable":
        return labels.get("endpoint", labels.get("service", "unknown-service"))
    if incident_type == "networkpolicy_block":
        # instance label looks like "http://demo-web.demo.svc.cluster.local"
        instance = labels.get("instance", "")
        return instance.split("//")[-1].split(".")[0] or "unknown-target"
    if incident_type == "replica_mismatch":
        return labels.get("deployment", "unknown-deployment")
    return labels.get("pod") or labels.get("node") or labels.get("deployment") or "unknown"


def classify_alert(alert: dict[str, Any]) -> ClassifiedAlert:
    labels = alert.get("labels", {}) or {}
    annotations = alert.get("annotations", {}) or {}
    incident_type = labels.get("incident_type", "unknown")
    return ClassifiedAlert(
        alertname=labels.get("alertname", "unknown"),
        incident_type=incident_type,
        namespace=labels.get("namespace", "demo"),
        resource_name=_resource_name_for(incident_type, labels),
        severity=labels.get("severity", "warning"),
        labels=labels,
        annotations=annotations,
    )
