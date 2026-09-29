"""
Structured step logging.

The whole point of this module is visibility: every time the agent sends
something out (a PromQL query, a LogQL query, a Bedrock request, a Kubernetes
API call) or receives something back, we print a clearly labelled block to
stdout AND persist a copy on the incident's timeline so the same information
shows up in the Approval UI. Nothing about the pipeline should be a black box.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("agent")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)

_ARROWS = {"SEND": "-->", "RECV": "<--", "INFO": "---", "ACTION": "==>"}

# In-memory ring of recent step events per incident, shown in the UI detail page.
_timelines: dict[int, list[dict[str, Any]]] = {}
_lock = threading.Lock()


def _truncate(value: Any, limit: int = 4000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, indent=2)
    if len(text) > limit:
        return text[: limit] + f"\n... [truncated, {len(text)} chars total]"
    return text


def log_step(
    incident_id: int | None,
    step: str,
    direction: str,
    payload: Any = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Log + record one pipeline step. direction: SEND | RECV | INFO | ACTION."""
    arrow = _ARROWS.get(direction, "---")
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    header = f"[{timestamp}] incident={incident_id or '-'} step={step} {arrow} {direction}"

    lines = [header]
    if note:
        lines.append(f"  note: {note}")
    body_text = None
    if payload is not None:
        body_text = _truncate(payload)
        for line in body_text.splitlines():
            lines.append(f"  | {line}")
    logger.info("\n".join(lines))

    event = {
        "timestamp": timestamp,
        "step": step,
        "direction": direction,
        "note": note,
        "payload": body_text,
    }
    if incident_id is not None:
        with _lock:
            _timelines.setdefault(incident_id, []).append(event)
    return event


def get_timeline(incident_id: int) -> list[dict[str, Any]]:
    with _lock:
        return list(_timelines.get(incident_id, []))
