"""Severity re-assessment node: purely rule-based (heuristics.py) from live
evidence rather than an LLM call - the signals here (restart counts, replica
gaps) are already precise numbers, so there's nothing for an LLM to add over
a threshold check, and it keeps this node deterministic/unit-testable like
the scaffold's supervisor.
"""
from __future__ import annotations

from typing import Any

from .. import heuristics
from ..state import IncidentState


async def severity(state: IncidentState) -> dict[str, Any]:
    computed_severity, rationale = heuristics.rule_based_severity(
        state["incident_type"], state.get("context", {}), state.get("alert_severity", "warning")
    )
    return {"severity": computed_severity, "severity_rationale": rationale}
