"""
Log-driven trigger: the second front door into the agent (the first being
Alertmanager metric alerts in webhook.py). A background task polls Loki for
error log lines in the watched namespaces and opens an incident for each new
error signature, feeding it into the SAME diagnosis/planning graph via
webhook.run_incident_pipeline.

Nothing here executes remediation - it only opens incidents. As everywhere
else, an incident becomes an action only after a human approves the plan in
the UI (see ui/routes.py, executor.py).
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from .config import settings
from .diagnosis.context import query_loki
from .diagnosis.log_classifier import classify_log_event
from .logging_utils import log_step, logger
from .webhook import run_incident_pipeline

# In-memory cooldown so the same recurring error line doesn't re-open an
# incident on every poll once its incident has moved past 'pending' (which is
# all audit.find_active_by_fingerprint dedups against). fingerprint -> epoch.
_seen: dict[str, float] = {}
_COOLDOWN_SECONDS = 900.0

_task: asyncio.Task | None = None

_TS_HEX = re.compile(r"\b(0x)?[0-9a-f]{4,}\b", re.IGNORECASE)
_DIGITS = re.compile(r"\d+")


def _signature(message: str) -> str:
    """Collapse a log line to a stable signature so 'connection refused to
    10.1.2.3:8080' and '...10.1.2.9:8081' dedup to the same incident."""
    lowered = message.strip().lower()
    lowered = _TS_HEX.sub("#", lowered)
    lowered = _DIGITS.sub("#", lowered)
    return lowered[:120]


def _logql_for(namespace: str) -> str:
    pattern = settings.log_error_pattern.replace("\\", "\\\\").replace('"', '\\"')
    return f'{{namespace="{namespace}"}} |~ "{pattern}"'


def _extract_events(loki_response: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Loki query_range response -> [(namespace, pod, message), ...]."""
    events: list[tuple[str, str, str]] = []
    try:
        streams = loki_response["data"]["result"]
    except (KeyError, TypeError):
        return events
    for stream in streams:
        labels = stream.get("stream", {}) or {}
        namespace = labels.get("namespace", "")
        pod = labels.get("pod") or labels.get("instance") or ""
        for value in stream.get("values", []) or []:
            if len(value) >= 2 and value[1]:
                events.append((namespace, pod, value[1]))
    return events


def _prune_cooldown(now: float) -> None:
    for fp, seen_at in list(_seen.items()):
        if now - seen_at > _COOLDOWN_SECONDS:
            del _seen[fp]


async def _poll_once() -> None:
    namespaces = [ns.strip() for ns in settings.log_watch_namespaces.split(",") if ns.strip()]
    now = time.time()
    _prune_cooldown(now)

    for namespace in namespaces:
        response = await asyncio.to_thread(
            query_loki, None, _logql_for(namespace), 50, settings.log_lookback
        )
        for ns, pod, message in _extract_events(response):
            fingerprint = f"log:{ns or namespace}:{pod}:{_signature(message)}"
            if fingerprint in _seen:
                continue
            _seen[fingerprint] = now

            classified = classify_log_event(ns or namespace, pod, message)
            raw_alert = {
                "labels": classified.labels,
                "annotations": classified.annotations,
                "fingerprint": fingerprint,
                "source": "loki",
            }
            try:
                result = await run_incident_pipeline(
                    fingerprint=fingerprint, classified=classified, raw_alert=raw_alert
                )
                log_step(
                    result.get("incident_id"),
                    "logwatch.trigger",
                    "INFO",
                    {"pod": pod, "incident_type": classified.incident_type, "message": message[:300]},
                    note=f"Opened incident from a Loki error log line (action={result.get('action')})",
                )
            except Exception as exc:  # noqa: BLE001 - one bad line must not kill the watcher
                logger.exception("log_watcher: failed to process error line: %s", exc)


async def _watch_loop() -> None:
    logger.info(
        "=== Log watcher started: polling Loki every %ss for /%s/ in namespaces [%s] ===",
        settings.log_poll_interval_seconds,
        settings.log_error_pattern,
        settings.log_watch_namespaces,
    )
    while True:
        try:
            await _poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the loop alive across transient Loki errors
            logger.exception("log_watcher: poll failed: %s", exc)
        await asyncio.sleep(settings.log_poll_interval_seconds)


def start() -> None:
    global _task
    if not settings.log_trigger_enabled:
        logger.info("Log watcher disabled (LOG_TRIGGER_ENABLED=false)")
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_watch_loop())


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
