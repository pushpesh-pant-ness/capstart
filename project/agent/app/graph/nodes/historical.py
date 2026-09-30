"""Historical retrieval node: past resolved/executed incidents of the same
type (audit.find_similar_resolved), re-scored against THIS incident's
resource/severity so the supervisor can tell a trustworthy replay candidate
apart from a same-type-but-different-resource one (heuristics.py).
"""
from __future__ import annotations

import json
from typing import Any

from ... import audit
from ...config import settings
from .. import heuristics
from ..state import IncidentState


def _parse(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


async def historical(state: IncidentState) -> dict[str, Any]:
    rows = audit.find_similar_resolved(
        state["incident_type"], exclude_id=state["incident_id"], limit=settings.diagnosis_history_examples
    )
    scored = []
    for row in rows:
        candidate = {
            "incident_id": row["id"],
            "resource_name": row.get("resource_name"),
            "computed_severity": row.get("computed_severity"),
            "status": row.get("status"),
            "remediation_plan": _parse(row.get("remediation_plan")),
        }
        candidate["similarity_score"] = heuristics.score_similar_incident(
            state["resource_name"], state.get("severity", ""), candidate
        )
        scored.append(candidate)
    scored.sort(key=lambda c: c["similarity_score"], reverse=True)
    return {"similar_incidents": scored}
