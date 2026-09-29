"""
AWS Bedrock client - Amazon Nova models, used ONLY to turn (alert + metrics +
logs) into a human-readable diagnosis paragraph. It never decides the
remediation action; that comes from the fixed templates in remediation/.

Uses the Bedrock Converse API, which is the recommended unified interface
for Nova models (amazon.nova-micro-v1:0 / amazon.nova-lite-v1:0 / amazon.nova-pro-v1:0).
"""
from __future__ import annotations

import json
from typing import Any

import boto3

from .config import settings
from .logging_utils import log_step

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
    return _client


_SYSTEM_PROMPT = (
    "You are an SRE assistant analyzing a Kubernetes incident. You are given the "
    "firing alert and supporting Prometheus/Loki context. Write a concise "
    "(4-6 sentence) human-readable diagnosis explaining the likely root cause and "
    "impact. Do not propose or mention specific remediation commands - a separate, "
    "deterministic system handles remediation; you only explain what is happening."
)


def _build_request(incident_type: str, alert: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    user_prompt = (
        f"Incident type: {incident_type}\n\n"
        f"Alert labels: {json.dumps(alert.get('labels', {}), default=str)}\n"
        f"Alert annotations: {json.dumps(alert.get('annotations', {}), default=str)}\n\n"
        f"Supporting context (Prometheus/Loki query results):\n"
        f"{json.dumps(context, default=str, indent=2)}"
    )
    return {
        "modelId": settings.bedrock_model_id,
        "system": [{"text": _SYSTEM_PROMPT}],
        "messages": [{"role": "user", "content": [{"text": user_prompt}]}],
        "inferenceConfig": {"maxTokens": 400, "temperature": 0.2, "topP": 0.9},
    }


def _fallback_text(incident_type: str, alert: dict[str, Any]) -> str:
    labels = alert.get("labels", {})
    return (
        f"[fallback diagnosis - Bedrock unavailable/disabled] "
        f"Incident type '{incident_type}' detected with labels {labels}. "
        f"See the attached Prometheus/Loki context panel for raw evidence."
    )


def generate_diagnosis(
    incident_id: int, incident_type: str, alert: dict[str, Any], context: dict[str, Any]
) -> str:
    if not settings.bedrock_enabled:
        text = _fallback_text(incident_type, alert)
        log_step(incident_id, "bedrock.converse", "INFO", text, note="Bedrock disabled via config, using fallback text")
        return text

    request = _build_request(incident_type, alert, context)
    log_step(
        incident_id,
        "bedrock.converse",
        "SEND",
        request,
        note=f"Requesting diagnosis text from Amazon Nova model '{settings.bedrock_model_id}'",
    )

    try:
        response = _get_client().converse(
            modelId=request["modelId"],
            system=request["system"],
            messages=request["messages"],
            inferenceConfig=request["inferenceConfig"],
        )
    except Exception as exc:  # noqa: BLE001 - never let a Bedrock outage break the pipeline
        log_step(incident_id, "bedrock.converse", "RECV", {"error": str(exc)}, note="Bedrock call failed, falling back")
        return _fallback_text(incident_type, alert)

    log_step(incident_id, "bedrock.converse", "RECV", response, note="Diagnosis response received from Bedrock")

    try:
        text = response["output"]["message"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        text = _fallback_text(incident_type, alert)
    return text
