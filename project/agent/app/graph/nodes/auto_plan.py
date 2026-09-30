"""Auto-plan shortcut: for a low-severity (P4) incident with a high-confidence
historical match on the SAME resource that actually recovered, replay that
incident's plan instead of spending an RCA + Plan LLM call reinventing it.
The target namespace/resource is still always re-locked to THIS incident,
never trusted from the historical row.
"""
from __future__ import annotations

from typing import Any
from datetime import datetime, timezone

from .supervisor import find_auto_plan_candidate
from ..state import IncidentState


async def auto_plan(state: IncidentState) -> dict[str, Any]:
    candidate = find_auto_plan_candidate(state)
    if candidate is None:
        # Shouldn't happen - route_after_supervisor only routes here when a
        # candidate exists - but escalate rather than silently planning nothing.
        return {"escalation_reason": "auto_plan: no matching historical candidate found"}

    source_plan = candidate.get("remediation_plan") or {}
    plan = {
        "title": source_plan.get("title", "Replayed remediation from a similar past incident"),
        "steps": source_plan.get("steps", []),
        "action": source_plan.get("action"),
        "target": {"namespace": state["namespace"], "name": state["resource_name"]},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return {
        "remediation_plan": plan,
        "diagnosis_text": (
            f"Auto-replayed from resolved incident #{candidate.get('incident_id')} "
            f"(same resource, same severity, similarity_score={candidate.get('similarity_score')})."
        ),
        "confidence_score": candidate.get("similarity_score", 0.0),
        "low_confidence": False,
    }
