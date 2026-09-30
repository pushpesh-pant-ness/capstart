"""Deterministic fallbacks + scoring - used when Bedrock is unavailable, and
for scoring how trustworthy a historical replay candidate is (auto_plan).
"""
from __future__ import annotations

from typing import Any


def _first_metric_value(query_result: dict[str, Any] | None) -> float | None:
    """Prometheus instant-query response -> first result's numeric value, or None."""
    try:
        result = query_result["data"]["result"]  # type: ignore[index]
        return float(result[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def rule_based_severity(incident_type: str, context: dict[str, Any], alert_severity: str) -> tuple[str, str]:
    """Independent P1-P4 re-assessment from live evidence, rather than just
    echoing back Alertmanager's static severity label."""
    if incident_type == "crashloop":
        restarts = _first_metric_value(context.get("restarts")) or 0
        if restarts >= 10:
            return "P1", f"{restarts:.0f} restarts in the last 5m - severe crash loop"
        if restarts >= 4:
            return "P2", f"{restarts:.0f} restarts in the last 5m"
        return "P3", f"{restarts:.0f} restarts in the last 5m - mild"

    if incident_type == "node_not_ready":
        return "P1", "A NotReady node stops all scheduling on it - treated as high impact"

    if incident_type == "service_unreachable":
        return "P1", "Zero available Service endpoints - likely a full outage for callers"

    if incident_type == "networkpolicy_block":
        return "P2", "Synthetic probe failing - partial connectivity loss"

    if incident_type == "replica_mismatch":
        spec = _first_metric_value(context.get("spec_replicas"))
        available = _first_metric_value(context.get("available_replicas"))
        unavailable = _first_metric_value(context.get("unavailable_replicas")) or 0
        if spec is not None and available == 0:
            return "P1", "Zero replicas available out of spec - full outage"
        if spec is not None and available is not None and available < spec:
            return "P2", f"{available:.0f}/{spec:.0f} replicas available"
        if unavailable > 0:
            return "P2", f"{unavailable:.0f} unavailable replica(s) (e.g. ImagePullBackOff on the surge pod)"
        return "P3", "Replica mismatch reported but current counts look close"

    # Unknown incident_type: fall back to whatever Alertmanager already said.
    mapped = {"critical": "P2", "warning": "P3"}.get(alert_severity, "P3")
    return mapped, f"No severity heuristic for incident_type='{incident_type}', using alert severity as-is"


def score_similar_incident(
    resource_name: str, severity: str, candidate: dict[str, Any]
) -> float:
    """Weighted heuristic - same resource (0.6), same recomputed severity (0.4).
    Deliberately stricter than title-overlap alone: replaying a plan against
    the wrong resource is the failure mode that matters here (see supervisor.py).
    """
    score = 0.0
    if resource_name and candidate.get("resource_name") == resource_name:
        score += 0.6
    if severity and candidate.get("computed_severity") == severity:
        score += 0.4
    return round(score, 3)
