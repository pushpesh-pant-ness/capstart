"""LangGraph orchestration for the diagnosis/planning pipeline.

The remediation ACTION is still always validated against a single
deterministic allow-listed action per incident_type (see guardrail.py) -
this graph lets the LLM reason its way to that action and author the
human-facing plan text, but a guardrail node rejects (-> escalate) anything
that doesn't match, so execution safety is unchanged from the old fixed
lookup-table design. Human approval (ui/routes.py) is still required
before the executor ever runs, regardless of which path produced the plan.
"""
from __future__ import annotations

from .graph import graph

__all__ = ["graph"]
