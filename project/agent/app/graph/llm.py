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


def _first_tool_use(response: dict) -> dict | None:
    try:
        blocks = response["output"]["message"]["content"]
    except (KeyError, TypeError):
        return None
    for block in blocks or []:
        if isinstance(block, dict) and "toolUse" in block:
            return block["toolUse"]
    return None


@traceable(name="llm.select_tool", run_type="llm")
def select_tool(
    incident_id: int,
    step: str,
    system_prompt: str,
    user_prompt: str,
    tool_specs: list[dict],
    max_tokens: int = 500,
) -> tuple[str, dict]:
    """Bedrock Converse with tool use: the model must CALL one of the provided
    tools rather than answer in free text. Returns (tool_name, tool_input).
    Raises LLMUnavailableError if Bedrock fails or the model answered with text
    instead of a tool call, so the caller can fall back."""
    if not settings.bedrock_enabled:
        raise LLMUnavailableError("Bedrock disabled via config")

    tool_names = [t["toolSpec"]["name"] for t in tool_specs]
    log_step(
        incident_id, step, "SEND",
        {"system": system_prompt, "user": user_prompt, "tools": tool_names},
        note=f"Offering {len(tool_names)} remediation tool(s) for the LLM to call: {tool_names}",
    )
    try:
        response = _get_client().converse(
            modelId=settings.bedrock_model_id,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_prompt}]}],
            toolConfig={"tools": tool_specs, "toolChoice": {"auto": {}}},
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0.2, "topP": 0.9},
        )
    except Exception as exc:  # noqa: BLE001 - never let a Bedrock outage crash the graph
        log_step(incident_id, step, "RECV", {"error": str(exc)}, note="Bedrock tool-use call failed")
        raise LLMUnavailableError(str(exc)) from exc

    tool_use = _first_tool_use(response)
    if tool_use is None or not tool_use.get("name"):
        log_step(incident_id, step, "RECV", response, note="Model did not return a tool call")
        raise LLMUnavailableError("model did not call a tool")

    name = tool_use["name"]
    tool_input = tool_use.get("input") or {}
    log_step(
        incident_id, step, "RECV",
        {"tool": name, "input": tool_input, "toolUseId": tool_use.get("toolUseId")},
        note=f"LLM chose remediation tool '{name}' via a tool call",
    )
    return name, tool_input
