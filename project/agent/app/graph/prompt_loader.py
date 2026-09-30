"""Loads the versioned prompt text files under app/graph/prompts/."""
from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).parent / "prompts"


def load(name: str) -> str:
    return (_DIR / name).read_text(encoding="utf-8")
