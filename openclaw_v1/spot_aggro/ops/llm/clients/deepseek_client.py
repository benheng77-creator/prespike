"""DeepSeek V3 client (via OpenRouter) — quantitative sanity role."""
from __future__ import annotations
from ..consensus import _call_member

PROVIDER = "openrouter"
MODEL = "deepseek/deepseek-chat-v3"
ROLE = "quant_sanity"


async def call(prompt: str) -> tuple[str, float]:
    return await _call_member(role=ROLE, provider=PROVIDER, model=MODEL, prompt=prompt)
