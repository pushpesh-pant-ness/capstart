"""RCA / diagnosis node: reuses bedrock_client.generate_diagnosis (already
does historical-example-grounded prompting + graceful Bedrock fallback) to
get both a human-readable diagnosis and a confidence score in one call, then
decides whether confidence is high enough to hand to the Plan node at all.
"""
from __future__ import annotations

from typing import Any

from ...bedrock_client import generate_diagnosis
from ..state import IncidentState

LOW_CONFIDENCE_THRESHOLD = 0.4


async def rca(state: IncidentState) -> dict[str, Any]:
    diagnosis = generate_diagnosis(
        state["incident_id"], state["incident_type"], state["raw_alert"], state.get("context", {})
    )
    low_confidence = diagnosis.confidence < LOW_CONFIDENCE_THRESHOLD
    result: dict[str, Any] = {
        "diagnosis_text": diagnosis.text,
        "confidence_score": diagnosis.confidence,
        "low_confidence": low_confidence,
    }
    if low_confidence:
        result["escalation_reason"] = (
            f"Diagnosis confidence {diagnosis.confidence:.2f} below threshold {LOW_CONFIDENCE_THRESHOLD}"
        )
    return result


def route_after_rca(state: IncidentState) -> str:
    return "escalate" if state.get("low_confidence") else "plan"
