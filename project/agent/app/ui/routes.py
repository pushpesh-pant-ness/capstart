"""
Approval UI - minimal FastAPI + HTMX. Pages:
  GET  /                      incident queue (auto-refreshes, active + resolved)
  GET  /cluster                live pod/node status across demo/monitoring/agent (auto-refreshes)
  GET  /incident/{id}         full detail: diagnosis, context, plan, timeline (auto-refreshes)
  POST /incident/{id}/approve kicks off the executor in the background (keeps retrying until resolved)
  POST /incident/{id}/reject  records rejection, no action taken
  POST /incident/{id}/retry   re-runs the executor for an incident that exhausted its retry budget
  POST /incident/{id}/reprocess  re-runs the diagnosis graph for an escalated incident
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import audit
from ..cluster_status import get_nodes, get_pods
from ..executor import execute_remediation
from ..graph import graph as diagnosis_graph
from ..graph.state import IncidentState
from ..logging_utils import get_timeline, log_step

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _parse_json_field(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _prepare(incident: dict[str, Any]) -> dict[str, Any]:
    incident = dict(incident)
    for field in ("raw_alert", "context_snapshot", "remediation_plan", "execution_result", "agent_evidence", "reflections"):
        incident[field] = _parse_json_field(incident.get(field))
    incident["duration_seconds"] = audit.duration_seconds(incident)
    return incident


_ACTIVE_STATUSES = {"pending", "approved", "in_progress", "escalated"}


def _run_remediation(incident_id: int, plan: dict[str, Any], namespace: str, resource_name: str) -> None:
    def on_update(status: str, result: dict[str, Any]) -> None:
        audit.update_incident(incident_id, status=status, execution_result=result)

    execute_remediation(incident_id, plan, namespace, resource_name, on_update=on_update)


@router.get("/", response_class=HTMLResponse)
def queue(request: Request) -> HTMLResponse:
    incidents = [_prepare(i) for i in audit.list_incidents(limit=200)]
    active_incidents = [i for i in incidents if i["status"] in _ACTIVE_STATUSES]
    resolved_incidents = [i for i in incidents if i["status"] not in _ACTIVE_STATUSES]
    return templates.TemplateResponse(
        "queue.html",
        {"request": request, "active_incidents": active_incidents, "resolved_incidents": resolved_incidents},
    )


@router.get("/cluster", response_class=HTMLResponse)
def cluster_status(request: Request) -> HTMLResponse:
    try:
        pods = sorted(get_pods(), key=lambda p: (p["namespace"], p["name"]))
        nodes = sorted(get_nodes(), key=lambda n: n["name"])
        error = None
    except Exception as exc:  # noqa: BLE001 - surface any k8s API error in the page itself
        pods, nodes, error = [], [], str(exc)
    return templates.TemplateResponse(
        "cluster.html", {"request": request, "pods": pods, "nodes": nodes, "error": error}
    )


@router.get("/incident/{incident_id}", response_class=HTMLResponse)
def incident_detail(request: Request, incident_id: int) -> HTMLResponse:
    incident = audit.get_incident(incident_id)
    if not incident:
        return HTMLResponse("Incident not found", status_code=404)
    incident = _prepare(incident)
    timeline = get_timeline(incident_id)
    return templates.TemplateResponse(
        "incident_detail.html", {"request": request, "incident": incident, "timeline": timeline}
    )


def _reprocess_stuck_incident(incident_id: int, incident: dict[str, Any], background_tasks: BackgroundTasks) -> None:
    """No usable action on the plan (e.g. the diagnosis graph never finished -
    killed mid-run, or an earlier bug left the row empty): re-run diagnosis
    from scratch instead of handing the executor a None action, which would
    just dead-end in execution_failed forever (see executor._HANDLERS lookup)."""
    log_step(
        incident_id, "ui.decision", "ACTION", {"decision": "reprocess_incomplete_plan"},
        note="Remediation plan has no action (diagnosis likely never completed) - re-running diagnosis instead",
    )
    raw_alert = _parse_json_field(incident.get("raw_alert")) or {}
    background_tasks.add_task(
        _reprocess, incident_id, raw_alert, incident["namespace"], incident["resource_name"], incident["incident_type"]
    )


@router.post("/incident/{incident_id}/approve")
def approve(
    incident_id: int, background_tasks: BackgroundTasks, approver: str = Form(default="human")
) -> RedirectResponse:
    incident = audit.get_incident(incident_id)
    if not incident:
        return HTMLResponse("Incident not found", status_code=404)  # type: ignore[return-value]

    plan = _parse_json_field(incident.get("remediation_plan")) or {}
    if not plan.get("action"):
        _reprocess_stuck_incident(incident_id, incident, background_tasks)
        return RedirectResponse(f"/incident/{incident_id}", status_code=303)

    log_step(
        incident_id, "ui.decision", "ACTION", {"decision": "approve", "by": approver},
        note="Human approved the remediation plan in the Approval UI",
    )
    now = datetime.now(timezone.utc).isoformat()
    audit.update_incident(incident_id, status="in_progress", decision_by=approver, decision_at=now)

    background_tasks.add_task(_run_remediation, incident_id, plan, incident["namespace"], incident["resource_name"])

    return RedirectResponse(f"/incident/{incident_id}", status_code=303)


@router.post("/incident/{incident_id}/retry")
def retry(incident_id: int, background_tasks: BackgroundTasks) -> RedirectResponse:
    """Re-run the executor for an incident whose retry budget was already exhausted."""
    incident = audit.get_incident(incident_id)
    if not incident:
        return HTMLResponse("Incident not found", status_code=404)  # type: ignore[return-value]

    plan = _parse_json_field(incident.get("remediation_plan")) or {}
    if not plan.get("action"):
        _reprocess_stuck_incident(incident_id, incident, background_tasks)
        return RedirectResponse(f"/incident/{incident_id}", status_code=303)

    log_step(incident_id, "ui.decision", "ACTION", {"decision": "retry"}, note="Human asked the agent to try again")
    audit.update_incident(incident_id, status="in_progress")

    background_tasks.add_task(_run_remediation, incident_id, plan, incident["namespace"], incident["resource_name"])

    return RedirectResponse(f"/incident/{incident_id}", status_code=303)


@router.post("/incident/{incident_id}/reject")
def reject(incident_id: int, approver: str = Form(default="human")) -> RedirectResponse:
    incident = audit.get_incident(incident_id)
    if not incident:
        return HTMLResponse("Incident not found", status_code=404)  # type: ignore[return-value]

    log_step(
        incident_id, "ui.decision", "ACTION", {"decision": "reject", "by": approver},
        note="Human rejected the remediation plan - no action will be taken",
    )
    now = datetime.now(timezone.utc).isoformat()
    audit.update_incident(incident_id, status="rejected", decision_by=approver, decision_at=now)

    return RedirectResponse(f"/incident/{incident_id}", status_code=303)


async def _reprocess(incident_id: int, raw_alert: dict[str, Any], namespace: str, resource_name: str, incident_type: str) -> None:
    initial_state: IncidentState = {
        "incident_id": incident_id,
        "incident_type": incident_type,
        "namespace": namespace,
        "resource_name": resource_name,
        "alert_severity": (raw_alert.get("labels", {}) or {}).get("severity", "warning"),
        "labels": raw_alert.get("labels", {}) or {},
        "annotations": raw_alert.get("annotations", {}) or {},
        "raw_alert": raw_alert,
    }
    final_state = await diagnosis_graph.ainvoke(initial_state)
    audit.update_incident(
        incident_id,
        context_snapshot=final_state.get("context"),
        diagnosis_text=final_state.get("diagnosis_text"),
        confidence_score=final_state.get("confidence_score"),
        computed_severity=final_state.get("severity"),
    )
    escalation_reason = final_state.get("escalation_reason")
    if escalation_reason:
        audit.update_incident(incident_id, status="escalated", escalation_reason=escalation_reason)
    else:
        audit.update_incident(
            incident_id, status="pending", escalation_reason=None, remediation_plan=final_state.get("remediation_plan")
        )


@router.post("/incident/{incident_id}/reprocess")
async def reprocess(incident_id: int, background_tasks: BackgroundTasks) -> RedirectResponse:
    """Re-run the diagnosis graph from scratch for an escalated incident (e.g. a
    transient Bedrock hiccup) instead of leaving it stuck for a human to fix by hand."""
    incident = audit.get_incident(incident_id)
    if not incident:
        return HTMLResponse("Incident not found", status_code=404)  # type: ignore[return-value]

    log_step(incident_id, "ui.decision", "ACTION", {"decision": "reprocess"}, note="Human asked the graph to re-run diagnosis")
    raw_alert = _parse_json_field(incident.get("raw_alert")) or {}
    background_tasks.add_task(
        _reprocess, incident_id, raw_alert, incident["namespace"], incident["resource_name"], incident["incident_type"]
    )
    return RedirectResponse(f"/incident/{incident_id}", status_code=303)
