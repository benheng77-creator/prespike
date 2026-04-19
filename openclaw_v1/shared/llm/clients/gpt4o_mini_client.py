"""GPT-4o-mini client — macro/news role."""
from __future__ import annotations
from ..consensus import _call_member

PROVIDER = "openai"
MODEL = "gpt-4o-mini"
ROLE = "macro"


async def call(prompt: str) -> tuple[str, float]:
    return await _call_member(role=ROLE, provider=PROVIDER, model=MODEL, prompt=prompt)
