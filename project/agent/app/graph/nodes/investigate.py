"""Investigate node: gathers the evidence the rest of the graph reasons over.

Two modes (settings.agentic_investigation):
  - deterministic (default): the fixed incident-type PromQL/LogQL fan-out in
    diagnosis/context.build_context.
  - agentic: a ReAct agent (react_investigator) additionally decides its own
    read-only diagnostics and writes a root-cause summary + evidence trail. The
    deterministic context is still gathered too, because the numeric severity
    thresholds downstream (heuristics.py) need those exact keys - the agent adds
    narrative/breadth on top, it doesn't replace the precise numbers.
"""
from __future__ import annotations

from typing import Any

from ...diagnosis.classifier import ClassifiedAlert
from ...diagnosis.context import build_context
from .. import react_investigator
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

    evidence_trail: list[dict[str, Any]] = []
    try:
        investigation = await react_investigator.investigate_agentically(
            incident_id=state["incident_id"],
            incident_type=state["incident_type"],
            namespace=state["namespace"],
            resource_name=state["resource_name"],
        )
        context["agent_investigation"] = investigation["summary"]
        evidence_trail = investigation["evidence_trail"]
    except react_investigator.InvestigationUnavailable:
        pass  # deterministic context above is the fallback

    return {"context": context, "evidence_trail": evidence_trail}
