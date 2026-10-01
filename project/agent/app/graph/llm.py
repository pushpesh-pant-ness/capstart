"""Single-purpose Bedrock wrapper for the plan-authoring node. Diagnosis text
(with confidence) already goes through bedrock_client.generate_diagnosis -
this is a second, smaller call used only to author the plan's action/title/
steps. Raises LLMUnavailableError on any failure so the calling node can
fall back to the deterministic template instead of crashing the graph.
"""
from __future__ import annotations

import boto3

from ..config import settings
from ..logging_utils import log_step
from ..observability import traceable

_client = None


class LLMUnavailableError(RuntimeError):
    """The Bedrock call failed, timed out, or returned unparseable output."""


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
    return _client


@traceable(name="llm.analyze", run_type="llm")
def analyze(incident_id: int, step: str, system_prompt: str, user_prompt: str, max_tokens: int = 400) -> str:
    if not settings.bedrock_enabled:
        raise LLMUnavailableError("Bedrock disabled via config")

    log_step(incident_id, step, "SEND", {"system": system_prompt, "user": user_prompt})
    try:
        response = _get_client().converse(
            modelId=settings.bedrock_model_id,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_prompt}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0.2, "topP": 0.9},
        )
    except Exception as exc:  # noqa: BLE001 - never let a Bedrock outage crash the graph
        log_step(incident_id, step, "RECV", {"error": str(exc)}, note="Bedrock call failed")
        raise LLMUnavailableError(str(exc)) from exc

    try:
        text = response["output"]["message"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        log_step(incident_id, step, "RECV", response, note="Unparseable Bedrock response")
        raise LLMUnavailableError(f"unparseable Bedrock response: {exc}") from exc

    if not text or not text.strip():
        raise LLMUnavailableError("Bedrock returned an empty response")
    log_step(incident_id, step, "RECV", {"text": text}, note="Response received from Bedrock")
    return text
