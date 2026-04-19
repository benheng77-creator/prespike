"""Gemini 2.5 Flash client — sentiment direction role."""
from __future__ import annotations
from ..consensus import _call_member

PROVIDER = "gemini"
MODEL = "gemini-2.5-flash"
ROLE = "sentiment"


async def call(prompt: str) -> tuple[str, float]:
    return await _call_member(role=ROLE, provider=PROVIDER, model=MODEL, prompt=prompt)
