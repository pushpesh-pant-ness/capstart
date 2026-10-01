"""Incident state schema threaded through every graph node (see graph.py)."""
from __future__ import annotations

from typing import Any, Optional, TypedDict


class IncidentState(TypedDict, total=False):
    # Identity / alert context - set once, before the graph runs.
    incident_id: int
    incident_type: str
    namespace: str
    resource_name: str
    alert_severity: str  # Alertmanager's own severity label (critical/warning)
    labels: dict[str, Any]
    annotations: dict[str, Any]
    raw_alert: dict[str, Any]

    # Investigate output
    context: dict[str, Any]

    # Agentic investigation (Phase 1): the ordered list of read-only tool calls
    # the investigate agent chose, plus their trimmed results - surfaced in the
    # UI/audit so the agent's reasoning path is visible. Empty when the
    # deterministic (non-agentic) investigate path ran.
    evidence_trail: list[dict[str, Any]]

    # Severity re-assessment (independent of the static alert_severity label)
    severity: str  # P1..P4
    severity_rationale: str

    # Historical retrieval - candidates already fetched by the caller
    # (webhook.py via audit.find_similar_resolved) and placed on the state.
    similar_incidents: list[dict[str, Any]]

    # Agentic supervisor (Phase 2): the LLM router's decision
    # (gather_more|auto_plan|full_rca|escalate) and its rationale, plus a hop
    # counter that bounds the gather_more -> investigate loop.
    router_decision: str
    router_rationale: str
    supervisor_hops: int

    # Agentic reflection (Phase 3): the self-critique verdicts recorded before
    # the guardrail, newest appended last.
    reflections: list[str]

    # RCA / diagnosis
    diagnosis_text: str
    confidence_score: float
    low_confidence: bool

    # Plan - same shape the executor already expects: {title, steps, action,
    # target: {namespace, name}, generated_at}
    remediation_plan: dict[str, Any]

    # Set by rca (low confidence) or guardrail (invalid plan); presence means
    # the incident ends at status=escalated instead of pending_approval.
    escalation_reason: Optional[str]
