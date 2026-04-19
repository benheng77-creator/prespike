"""Claude Haiku 4.5 client — pattern validator role."""
from __future__ import annotations
from typing import Any
from ..consensus import _call_member

PROVIDER = "anthropic"
MODEL = "claude-haiku-4-5"
ROLE = "pattern"


async def call(prompt: str) -> tuple[str, float]:
    """Return (text, cost_usd)."""
    return await _call_member(role=ROLE, provider=PROVIDER, model=MODEL, prompt=prompt)
