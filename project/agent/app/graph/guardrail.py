"""Deterministic plan sanity check, run after the LLM (or the historical
auto_plan replay) proposes a plan and before a human ever sees it. This is
the actual safety boundary: the LLM may author the plan text and pick which
tool to apply, but the ACTION it names must be one of the tools allow-listed
for this incident_type (see remediation/templates.py), and the target must be
exactly the namespace/resource the alert fired for - never a value the LLM
invented. Anything that fails this check escalates instead of reaching the
approval queue.
"""
from __future__ import annotations

from typing import Any, Optional

from ..remediation.templates import TEMPLATES, allowed_actions


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

    if TEMPLATES.get(incident_type) is None:
        return f"no allow-listed action registered for incident_type '{incident_type}'"
    allowed = allowed_actions(incident_type)
    if plan["action"] not in allowed:
        return (
            f"LLM proposed action '{plan['action']}' but the allow-listed actions "
            f"for incident_type '{incident_type}' are {sorted(allowed)}"
        )

    target = plan.get("target") or {}
    if target.get("namespace") != expected_namespace or target.get("name") != expected_resource_name:
        return (
            f"plan target {target} does not match the alert's actual "
            f"namespace/resource ({expected_namespace}/{expected_resource_name})"
        )
    return None
