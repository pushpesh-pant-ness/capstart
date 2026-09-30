"""Supervisor node (pure rule-based router, no LLM): decides whether a P4
incident with a trustworthy historical match can shortcut straight to
auto_plan, or whether it needs the full RCA -> Plan path.
"""
from __future__ import annotations

from typing import Any, Optional

from ..state import IncidentState

AUTO_PLAN_SEVERITY = "P4"
AUTO_PLAN_SIMILARITY_THRESHOLD = 0.7  # requires both same-resource AND same-severity match


def find_auto_plan_candidate(state: IncidentState) -> Optional[dict[str, Any]]:
    if state.get("severity") != AUTO_PLAN_SEVERITY:
        return None
    for candidate in state.get("similar_incidents") or []:
        if candidate.get("similarity_score", 0.0) < AUTO_PLAN_SIMILARITY_THRESHOLD:
            continue
        if not candidate.get("remediation_plan"):
            continue
        return candidate
    return None


async def supervisor(state: IncidentState) -> Optional[dict[str, Any]]:
    # No-op node - routing happens in route_after_supervisor. Must return
    # None, not {}: LangGraph treats an empty dict as an invalid update.
    return None


def route_after_supervisor(state: IncidentState) -> str:
    return "auto_plan" if find_auto_plan_candidate(state) is not None else "rca"
