"""Guardrail node: deterministic plan sanity check run after either planner
(auto_plan or plan), before a plan is persisted as pending_approval.
"""
from __future__ import annotations

from typing import Any, Optional

from langgraph.graph import END

from .. import guardrail
from ..state import IncidentState


async def guardrail_node(state: IncidentState) -> Optional[dict[str, Any]]:
    # None (not {}) when the plan passes - LangGraph rejects an empty dict as
    # an invalid node update.
    reason = guardrail.check_plan(
        state.get("remediation_plan") or {},
        incident_type=state["incident_type"],
        expected_namespace=state["namespace"],
        expected_resource_name=state["resource_name"],
    )
    return {"escalation_reason": reason} if reason else None


def route_after_guardrail(state: IncidentState) -> str:
    return "escalate" if state.get("escalation_reason") else END
