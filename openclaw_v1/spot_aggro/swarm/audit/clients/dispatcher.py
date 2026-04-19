"""
Spot-local LLM dispatcher for the audit swarm.

Copy-forked from the spot slice of `shared/llm/consensus.py` per Phase 1
§8.2. The shared file is untouched; spot does not share a mutable helper
with the other engine.

Contract:
    call(role, provider, model, prompt, timeout_s, max_tokens) ->
        (text, cost_usd, latency_ms)

    RAISES:
        asyncio.TimeoutError            timeout exceeded
        LLMProviderError (wraps exc)    unknown provider, auth failure, parse
                                        failure at transport layer

This module never parses LLM output as a domain-level decision — that's
the role-adapter's job. It is a pure transport + token-budget + clock
wrapper.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

log = logging.getLogger("spot_aggro.audit_swarm.dispatcher")


class LLMProviderError(RuntimeError):
    """Transport or provider error (not a domain REJECT)."""


# Per-provider spot-facing clients. Each exports `async def call(prompt) ->
# (text, cost_usd)`. We import these as transport-only adapters; the
# adapter modules themselves have no spot/apex-specific decision logic.
def _provider_call_fn(provider: str, model: str) -> Callable[..., object]:
    """Return the transport coroutine for (provider, model).

    Imports are lazy so missing optional deps only break the specific role
    that needed them, not the whole swarm module.
    """
    key = (provider, model)
    if provider == "anthropic" and "haiku" in model:
        from shared.llm.clients.haiku_client import call as fn
        return fn
    if provider == "anthropic" and "opus" in model:
        from shared.llm.clients.opus_client import call as fn
        return fn
    if provider == "openai":
        from shared.llm.clients.gpt4o_mini_client import call as fn
        return fn
    if provider == "gemini":
        from shared.llm.clients.gemini_flash_client import call as fn
        return fn
    if provider == "openrouter":
        from shared.llm.clients.deepseek_client import call as fn
        return fn
    if provider == "mistral":
        from shared.llm.clients.mistral_small_client import call as fn
        return fn
    raise LLMProviderError(
        f"unknown provider/model for audit swarm: {key!r}"
    )


async def call(
    *,
    role: str,
    provider: str,
    model: str,
    prompt: str,
    timeout_s: float,
) -> tuple[str, float, int]:
    """Call the provider with `prompt`. Returns (text, cost_usd, latency_ms).

    Raises asyncio.TimeoutError on timeout (Q1 enforcement downstream).
    """
    fn = _provider_call_fn(provider, model)
    started = time.time()
    text, cost = await asyncio.wait_for(fn(prompt), timeout=timeout_s)
    latency_ms = int((time.time() - started) * 1000)
    log.debug(
        "audit_swarm dispatch role=%s provider=%s model=%s latency=%dms cost=$%.5f",
        role, provider, model, latency_ms, cost,
    )
    return text or "", float(cost or 0.0), latency_ms
