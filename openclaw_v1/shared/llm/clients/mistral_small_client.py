"""Mistral Small 3 client — outlier/manipulation detector role."""
from __future__ import annotations
from ..consensus import _call_member

PROVIDER = "mistral"
MODEL = "mistral-small-latest"
ROLE = "outlier"


async def call(prompt: str) -> tuple[str, float]:
    return await _call_member(role=ROLE, provider=PROVIDER, model=MODEL, prompt=prompt)
