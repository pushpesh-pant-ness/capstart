"""Investigate node: reuses the existing incident-type-specific Prometheus/
Loki context gathering (diagnosis/context.py) as the graph's first step.
"""
from __future__ import annotations

from typing import Any

from ...diagnosis.classifier import ClassifiedAlert
from ...diagnosis.context import build_context
from ..state import IncidentState


async def investigate(state: IncidentState) -> dict[str, Any]:
    classified = ClassifiedAlert(
        alertname=state["raw_alert"].get("labels", {}).get("alertname", "unknown"),
        incident_type=state["incident_type"],
        namespace=state["namespace"],
        resource_name=state["resource_name"],
        severity=state.get("alert_severity", "warning"),
        labels=state.get("labels", {}),
        annotations=state.get("annotations", {}),
    )
    context = build_context(state["incident_id"], classified)
    return {"context": context}
