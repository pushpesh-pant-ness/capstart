"""Escalation node: terminal for anything the graph can't confidently
propose a plan for (low RCA confidence, or a guardrail rejection).
"""
from __future__ import annotations

from typing import Any

from ..state import IncidentState


async def escalate(state: IncidentState) -> dict[str, Any]:
    return {"escalation_reason": state.get("escalation_reason") or "escalated: reason not recorded"}
