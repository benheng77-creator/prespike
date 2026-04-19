"""
Claw commentary — AI-generated narrative layer.

Every output of this module is tagged ``source="claw.commentary"`` and
``authoritative=False``. Commentary NEVER enters execution or scoring. The
UI must render it in a visibly non-authoritative zone.

Delegates to backtest_plus.gemini_adapter for transport + schema. This
module is the contract-safe wrapper that stamps the tags.
"""

from __future__ import annotations

from typing import Any, Mapping


COMMENTARY_TAGS = {
    "source": "claw.commentary",
    "authoritative": False,
    "nic_version": "CLAW-NIC-v1",
    "disclaimer": (
        "Commentary is advisory narrative only. It is NEVER consulted by the "
        "bot brain, scoring, or execution path. Treat as read-only context."
    ),
}


def _tag(out: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(out or {})
    merged.setdefault("fields", {})
    merged.update(COMMENTARY_TAGS)
    return merged


def annotate_run(run_payload: Mapping[str, Any]) -> dict[str, Any]:
    from backtest_plus.gemini_adapter import annotate_run as _call
    return _tag(_call(dict(run_payload)))


def synthesize_scenario(brief: str) -> dict[str, Any]:
    from backtest_plus.gemini_adapter import synthesize_scenario as _call
    return _tag(_call(str(brief)))


def suggest_blend(goal: str) -> dict[str, Any]:
    from backtest_plus.gemini_adapter import suggest_blend as _call
    return _tag(_call(str(goal)))


def detect_anomalies(run_payload: Mapping[str, Any]) -> dict[str, Any]:
    from backtest_plus.gemini_adapter import detect_anomalies as _call
    return _tag(_call(dict(run_payload)))


def full_report(run_payload: Mapping[str, Any]) -> dict[str, Any]:
    from backtest_plus.gemini_adapter import full_report as _call
    return _tag(_call(dict(run_payload)))


def narrate(
    prompt: str,
    *,
    provider: str = "auto",
    max_tokens: int = 500,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """Generic commentary generator — routes through any configured provider.

    Picks from OpenAI / Anthropic / Gemini / Mistral / OpenRouter depending
    on which API keys are set. Always tags the result as commentary only.
    """
    from . import llm as _llm
    out = _llm.complete(
        prompt, provider=provider,
        max_tokens=max_tokens, temperature=temperature,
    )
    return _tag(out)


def list_providers() -> dict[str, Any]:
    """Enumerate LLM providers currently configured (keys present)."""
    from . import llm as _llm
    return {
        "providers": _llm.available_providers(),
        "source": "claw.commentary",
        "authoritative": False,
    }
