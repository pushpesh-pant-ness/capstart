"""Historical retrieval node: past resolved/executed incidents re-scored
against THIS incident's resource/severity so the supervisor can tell a
trustworthy replay candidate apart from a same-type-but-different-resource
one (heuristics.py).

Two retrieval modes (settings.hybrid_retrieval_enabled):
  - keyword (default): exact same-type match (audit.find_similar_resolved).
  - hybrid: Bedrock embeds this incident's symptoms and pgvector returns the
    semantically-nearest resolved incidents (across types); the same
    resource/severity heuristic then re-ranks them, blended with the vector
    similarity. Falls back to the keyword path if embedding is unavailable.
"""
from __future__ import annotations

import json
from typing import Any

from ... import audit
from ...config import settings
from ... import bedrock_client
from .. import heuristics
from ..state import IncidentState


def _parse(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _candidate(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "incident_id": row["id"],
        "resource_name": row.get("resource_name"),
        "computed_severity": row.get("computed_severity"),
        "status": row.get("status"),
        "remediation_plan": _parse(row.get("remediation_plan")),
    }


def _symptom_text(state: IncidentState) -> str:
    """Compact, deterministic description of the incident's symptoms - the
    embedding input. Built only from signal available at this node (no
    diagnosis_text yet), so it stays symmetric with stored embeddings."""
    annotations = state.get("annotations") or {}
    parts = [
        f"incident_type: {state['incident_type']}",
        f"resource: {state.get('namespace')}/{state.get('resource_name')}",
        f"severity: {state.get('severity', '')}",
        f"summary: {annotations.get('summary', '')}",
        f"description: {annotations.get('description', '')}",
    ]
    return "\n".join(p for p in parts if p.rsplit(": ", 1)[-1])


def _hybrid_candidates(state: IncidentState) -> list[dict[str, Any]] | None:
    """Returns re-ranked candidates via pgvector, or None to signal the caller
    to fall back to the keyword path (embedding unavailable)."""
    incident_id = state["incident_id"]
    embedding = bedrock_client.embed_text(incident_id, _symptom_text(state))
    if not embedding:
        return None
    audit.set_incident_embedding(incident_id, embedding)  # searchable for future incidents
    rows = audit.find_similar_resolved_hybrid(
        embedding, exclude_id=incident_id, limit=settings.vector_recall_limit
    )
    alpha = settings.hybrid_vector_weight
    scored = []
    for row in rows:
        candidate = _candidate(row)
        heuristic = heuristics.score_similar_incident(
            state["resource_name"], state.get("severity", ""), candidate
        )
        vector_similarity = max(0.0, min(1.0, float(row.get("vector_similarity") or 0.0)))
        candidate["vector_similarity"] = round(vector_similarity, 3)
        candidate["similarity_score"] = round(alpha * vector_similarity + (1 - alpha) * heuristic, 3)
        scored.append(candidate)
    return scored


def _keyword_candidates(state: IncidentState) -> list[dict[str, Any]]:
    rows = audit.find_similar_resolved(
        state["incident_type"], exclude_id=state["incident_id"], limit=settings.diagnosis_history_examples
    )
    scored = []
    for row in rows:
        candidate = _candidate(row)
        candidate["similarity_score"] = heuristics.score_similar_incident(
            state["resource_name"], state.get("severity", ""), candidate
        )
        scored.append(candidate)
    return scored


async def historical(state: IncidentState) -> dict[str, Any]:
    scored = None
    if settings.hybrid_retrieval_enabled and settings.bedrock_enabled:
        scored = _hybrid_candidates(state)
    if scored is None:
        scored = _keyword_candidates(state)
    scored.sort(key=lambda c: c["similarity_score"], reverse=True)
    return {"similar_incidents": scored}
