"""
Webhook receiver: this is the front door of the agent. Alertmanager POSTs
here whenever an alert rule fires or resolves (see monitoring/prometheus/
alertmanager-config.yaml). Every incoming payload is logged in full, then
each alert is classified and handed to the diagnosis/planning graph
(app/graph) - investigate -> severity -> historical -> supervisor ->
(auto_plan | rca -> plan) -> guardrail -> pending_approval or escalated.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from . import audit
from .diagnosis.classifier import ClassifiedAlert, classify_alert
from .graph import graph
from .graph.state import IncidentState
from .logging_utils import log_step
from .observability import traceable

router = APIRouter()


@router.post("/webhook/alertmanager")
async def receive_alertmanager_webhook(request: Request) -> dict[str, Any]:
    payload = await request.json()
    log_step(
        None,
        "webhook.receive",
        "RECV",
        payload,
        note=f"Alertmanager webhook POST, receiver={payload.get('receiver')}, "
        f"{len(payload.get('alerts', []))} alert(s)",
    )

    results = []
    for alert in payload.get("alerts", []):
        results.append(await _process_alert(alert))
    return {"processed": results}


@traceable(name="process_alert", run_type="chain")
async def _process_alert(alert: dict[str, Any]) -> dict[str, Any]:
    fingerprint = alert.get("fingerprint", "")
    status = alert.get("status", "firing")

    if status == "resolved":
        existing = audit.find_active_by_fingerprint(fingerprint)
        if existing:
            audit.update_incident(existing["id"], status="resolved")
            log_step(
                existing["id"], "alert.resolved", "INFO", alert,
                note="Alertmanager reports this alert is resolved",
            )
            return {"incident_id": existing["id"], "action": "marked_resolved"}
        return {"incident_id": None, "action": "resolved_no_matching_incident"}

    existing = audit.find_active_by_fingerprint(fingerprint)
    if existing:
        log_step(
            existing["id"], "alert.duplicate", "INFO", alert,
            note="Already have a pending incident for this fingerprint, not re-processing",
        )
        return {"incident_id": existing["id"], "action": "duplicate_ignored"}

    classified = classify_alert(alert)
    return await run_incident_pipeline(fingerprint=fingerprint, classified=classified, raw_alert=alert)


@traceable(name="run_incident_pipeline", run_type="chain")
async def run_incident_pipeline(
    *, fingerprint: str, classified: ClassifiedAlert, raw_alert: dict[str, Any]
) -> dict[str, Any]:
    """Shared path for every incident source (Alertmanager metric alerts and
    the Loki log watcher, see app/log_watcher.py): persist the incident, run
    the diagnosis/planning graph, and land it as pending_approval or escalated.
    The graph never executes anything - remediation only runs after a human
    approves in the UI (see ui/routes.py, executor.py)."""
    incident_id = audit.create_incident(
        fingerprint=fingerprint,
        alertname=classified.alertname,
        incident_type=classified.incident_type,
        namespace=classified.namespace,
        resource_name=classified.resource_name,
        severity=classified.severity,
        raw_alert=raw_alert,
    )
    log_step(
        incident_id, "webhook.classify", "INFO", classified.__dict__,
        note="Rule engine classified the alert into an incident type",
    )

    initial_state: IncidentState = {
        "incident_id": incident_id,
        "incident_type": classified.incident_type,
        "namespace": classified.namespace,
        "resource_name": classified.resource_name,
        "alert_severity": classified.severity,
        "labels": classified.labels,
        "annotations": classified.annotations,
        "raw_alert": raw_alert,
    }
    final_state = await graph.ainvoke(initial_state)

    audit.update_incident(
        incident_id,
        context_snapshot=final_state.get("context"),
        diagnosis_text=final_state.get("diagnosis_text"),
        confidence_score=final_state.get("confidence_score"),
        computed_severity=final_state.get("severity"),
        agent_evidence=final_state.get("evidence_trail") or [],
        router_decision=final_state.get("router_decision"),
        router_rationale=final_state.get("router_rationale"),
        reflections=final_state.get("reflections") or [],
    )

    escalation_reason = final_state.get("escalation_reason")
    if escalation_reason:
        audit.update_incident(incident_id, status="escalated", escalation_reason=escalation_reason)
        log_step(
            incident_id, "graph.escalate", "INFO", {"reason": escalation_reason},
            note="Diagnosis graph escalated this incident - no plan will be shown for approval",
        )
        return {"incident_id": incident_id, "action": "escalated"}

    plan = final_state.get("remediation_plan")
    audit.update_incident(incident_id, remediation_plan=plan)
    log_step(
        incident_id, "remediation.plan_ready", "INFO", plan,
        note="Plan passed the guardrail check - awaiting human approval in the UI",
    )

    return {"incident_id": incident_id, "action": "created_pending_approval"}
