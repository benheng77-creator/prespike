"""Claude Opus 4.6 client — veto authority, only fired on ensemble conflict > 0.70."""
from __future__ import annotations
from ..consensus import _call_member

PROVIDER = "anthropic"
MODEL = "claude-opus-4-6"
ROLE = "veto"


async def call(prompt: str) -> tuple[str, float]:
    return await _call_member(role=ROLE, provider=PROVIDER, model=MODEL, prompt=prompt)
