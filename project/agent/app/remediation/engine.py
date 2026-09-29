"""Turn a classified alert into a concrete, deterministic remediation plan."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..diagnosis.classifier import ClassifiedAlert
from .templates import TEMPLATES


def build_plan(classified: ClassifiedAlert) -> dict[str, Any]:
    template = TEMPLATES.get(classified.incident_type)
    if not template:
        return {
            "title": "No remediation template available",
            "steps": ["Unrecognized incident_type; manual investigation required."],
            "action": "manual",
            "target": {"namespace": classified.namespace, "name": classified.resource_name},
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    return {
        "title": template["title"],
        "steps": template["steps"],
        "action": template["action"],
        "target": {"namespace": classified.namespace, "name": classified.resource_name},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
