"""
Webhook receiver: this is the front door of the agent. Alertmanager POSTs
here whenever an alert rule fires or resolves (see monitoring/prometheus/
alertmanager-config.yaml). Every incoming payload is logged in full, then
each alert is classified, given supporting context, diagnosed via Bedrock,
and turned into a pending remediation plan awaiting human approval.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from . import audit
from .bedrock_client import generate_diagnosis
from .diagnosis.classifier import classify_alert
from .diagnosis.context import build_context
from .logging_utils import log_step
from .remediation.engine import build_plan

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

    results = [_process_alert(alert) for alert in payload.get("alerts", [])]
    return {"processed": results}


def _process_alert(alert: dict[str, Any]) -> dict[str, Any]:
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
    incident_id = audit.create_incident(
        fingerprint=fingerprint,
        alertname=classified.alertname,
        incident_type=classified.incident_type,
        namespace=classified.namespace,
        resource_name=classified.resource_name,
        severity=classified.severity,
        raw_alert=alert,
    )
    log_step(
        incident_id, "webhook.classify", "INFO", classified.__dict__,
        note="Rule engine classified the alert into an incident type",
    )

    context = build_context(incident_id, classified)
    audit.update_incident(incident_id, context_snapshot=context)

    diagnosis_text = generate_diagnosis(incident_id, classified.incident_type, alert, context)
    audit.update_incident(incident_id, diagnosis_text=diagnosis_text)

    plan = build_plan(classified)
    audit.update_incident(incident_id, remediation_plan=plan)
    log_step(
        incident_id, "remediation.plan_ready", "INFO", plan,
        note="Deterministic remediation plan drafted - awaiting human approval in the UI",
    )

    return {"incident_id": incident_id, "action": "created_pending_approval"}
