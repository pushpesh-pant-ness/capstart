"""Deterministic plan sanity check, run after the LLM (or the historical
auto_plan replay) proposes a plan and before a human ever sees it. This is
the actual safety boundary: the LLM may author the plan text, but the
ACTION it names must match the single action this incident_type's executor
handler actually supports (see remediation/templates.py), and the target
must be exactly the namespace/resource the alert fired for - never a value
the LLM invented. Anything that fails this check escalates instead of
reaching the approval queue.
"""
from __future__ import annotations

from typing import Any, Optional

from ..remediation.templates import TEMPLATES


def check_plan(
    plan: dict[str, Any],
    *,
    incident_type: str,
    expected_namespace: str,
    expected_resource_name: str,
) -> Optional[str]:
    """Returns None if the plan is safe to show a human, else an escalation reason."""
    if not plan.get("action"):
        return "plan is missing an 'action'"
    if not plan.get("title") or not plan.get("steps"):
        return "plan is missing a title/steps explanation"

    template = TEMPLATES.get(incident_type)
    if template is None:
        return f"no allow-listed action registered for incident_type '{incident_type}'"
    if plan["action"] != template["action"]:
        return (
            f"LLM proposed action '{plan['action']}' but the only allow-listed action "
            f"for incident_type '{incident_type}' is '{template['action']}'"
        )

    target = plan.get("target") or {}
    if target.get("namespace") != expected_namespace or target.get("name") != expected_resource_name:
        return (
            f"plan target {target} does not match the alert's actual "
            f"namespace/resource ({expected_namespace}/{expected_resource_name})"
        )
    return None
