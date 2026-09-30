"""
Optional LangSmith tracing around the diagnosis pipeline (context gathering +
Bedrock calls). Fully opt-in and fails safe: if `langsmith` isn't installed,
or LANGSMITH_ENABLED isn't set, `traceable` is a no-op decorator and every
wrapped function runs exactly as it would otherwise - mirrors the fallback
pattern already used for Bedrock in bedrock_client.py.

Enable with (see https://docs.smith.langchain.com/):
  LANGSMITH_ENABLED=true
  LANGSMITH_API_KEY=<your key>
  LANGSMITH_PROJECT=capstart-remediation-agent   # optional, has a default
"""
from __future__ import annotations

import os
from typing import Any, Callable, TypeVar

from .config import settings
from .logging_utils import logger

F = TypeVar("F", bound=Callable[..., Any])


def _noop_traceable(*_args: Any, **_kwargs: Any) -> Callable[[F], F]:
    def decorator(func: F) -> F:
        return func

    return decorator


traceable: Callable[..., Callable[[F], F]] = _noop_traceable

if settings.langsmith_enabled:
    try:
        from langsmith import traceable as _langsmith_traceable

        os.environ.setdefault("LANGSMITH_TRACING", "true")
        os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)
        traceable = _langsmith_traceable
        logger.info(f"=== LangSmith tracing enabled (project={settings.langsmith_project}) ===")
    except ImportError:
        logger.warning(
            "LANGSMITH_ENABLED=true but the 'langsmith' package is not installed - tracing disabled"
        )
