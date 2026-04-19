"""
Claw LLM — unified multi-provider completion wrapper.

Supported providers (selected by env var presence):
    openai       OPENAI_API_KEY         → https://api.openai.com/v1
    anthropic    ANTHROPIC_API_KEY      → https://api.anthropic.com/v1
    gemini       GEMINI_API_KEY         → generativelanguage.googleapis.com
    mistral      MISTRAL_API_KEY        → https://api.mistral.ai/v1
    openrouter   OPENROUTER_API_KEY     → https://openrouter.ai/api/v1

Non-interference contract:
    * This module is commentary / analysis only. It NEVER returns a trade
      signal, score, or decision.
    * Outputs are passed through claw.commentary.* for UI rendering, which
      stamps them as non-authoritative.
    * Failures return a fallback dict — never raise.
    * No state kept between calls. Purely stateless.

Typical usage:
    from claw.llm import complete
    out = complete("Summarise this run in 80 words: ...", provider="auto")
    if out["ok"]:
        text = out["text"]
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional


log = logging.getLogger("claw.llm")


PROVIDER_MODELS = {
    "openai":     "gpt-4o-mini",
    "anthropic":  "claude-3-5-haiku-20241022",
    "gemini":     "gemini-2.5-flash",
    "mistral":    "mistral-small-latest",
    "openrouter": "mistralai/mistral-small",
}


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def available_providers() -> list[str]:
    """Return the provider ids with credentials set."""
    out = []
    if _env("OPENAI_API_KEY"):     out.append("openai")
    if _env("ANTHROPIC_API_KEY"):  out.append("anthropic")
    if _env("GEMINI_API_KEY"):     out.append("gemini")
    if _env("MISTRAL_API_KEY"):    out.append("mistral")
    if _env("OPENROUTER_API_KEY"): out.append("openrouter")
    return out


def _pick_provider(preferred: str) -> str:
    """Return the actual provider to call given a preferred name.

    'auto' picks the first available in preference order:
    gemini (cheap & fast) → mistral → openai → anthropic → openrouter.
    """
    have = set(available_providers())
    if preferred != "auto" and preferred in have:
        return preferred
    for candidate in ("gemini", "mistral", "openai", "anthropic", "openrouter"):
        if candidate in have:
            return candidate
    return ""      # no provider available


def _post(url: str, body: dict, headers: dict, timeout: float = 20.0) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"content-type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body_txt = e.read().decode("utf-8", errors="replace")
        except Exception:
            body_txt = ""
        return e.code, body_txt
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Per-provider callers
# ---------------------------------------------------------------------------

def _call_openai(prompt: str, *, model: str, max_tokens: int, temperature: float) -> dict:
    code, body = _post(
        "https://api.openai.com/v1/chat/completions",
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        headers={"Authorization": f"Bearer {_env('OPENAI_API_KEY')}"},
    )
    if code != 200:
        return {"ok": False, "reason": f"http {code}", "raw": body[:400]}
    try:
        data = json.loads(body)
        text = data["choices"][0]["message"]["content"]
        return {"ok": True, "text": text, "model": model, "provider": "openai"}
    except Exception as exc:
        return {"ok": False, "reason": f"parse: {type(exc).__name__}", "raw": body[:400]}


def _call_anthropic(prompt: str, *, model: str, max_tokens: int, temperature: float) -> dict:
    code, body = _post(
        "https://api.anthropic.com/v1/messages",
        {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        },
        headers={
            "x-api-key": _env("ANTHROPIC_API_KEY"),
            "anthropic-version": "2023-06-01",
        },
    )
    if code != 200:
        return {"ok": False, "reason": f"http {code}", "raw": body[:400]}
    try:
        data = json.loads(body)
        blocks = data.get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return {"ok": True, "text": text, "model": model, "provider": "anthropic"}
    except Exception as exc:
        return {"ok": False, "reason": f"parse: {type(exc).__name__}", "raw": body[:400]}


def _call_gemini(prompt: str, *, model: str, max_tokens: int, temperature: float) -> dict:
    key = _env("GEMINI_API_KEY")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    # 2.5-flash/pro charge thinking tokens against maxOutputTokens, which silently
    # truncates the real output. Disable thinking for structured-output calls so
    # the full token budget goes to the JSON we asked for.
    gen_cfg: dict[str, Any] = {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
        "responseMimeType": "application/json",
    }
    if "2.5" in model:
        gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
    code, body = _post(
        url,
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": gen_cfg,
        },
        headers={},
    )
    if code != 200:
        return {"ok": False, "reason": f"http {code}", "raw": body[:400]}
    try:
        data = json.loads(body)
        cand = data.get("candidates", [{}])[0]
        parts = cand.get("content", {}).get("parts", []) or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        finish = cand.get("finishReason", "")
        if not text:
            return {
                "ok": False,
                "reason": f"empty_text (finish={finish})",
                "raw": body[:400],
            }
        return {
            "ok": True, "text": text, "model": model, "provider": "gemini",
            "finish_reason": finish,
        }
    except Exception as exc:
        return {"ok": False, "reason": f"parse: {type(exc).__name__}", "raw": body[:400]}


def _call_mistral(prompt: str, *, model: str, max_tokens: int, temperature: float) -> dict:
    code, body = _post(
        "https://api.mistral.ai/v1/chat/completions",
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        headers={"Authorization": f"Bearer {_env('MISTRAL_API_KEY')}"},
    )
    if code != 200:
        return {"ok": False, "reason": f"http {code}", "raw": body[:400]}
    try:
        data = json.loads(body)
        text = data["choices"][0]["message"]["content"]
        return {"ok": True, "text": text, "model": model, "provider": "mistral"}
    except Exception as exc:
        return {"ok": False, "reason": f"parse: {type(exc).__name__}", "raw": body[:400]}


def _call_openrouter(prompt: str, *, model: str, max_tokens: int, temperature: float) -> dict:
    code, body = _post(
        "https://openrouter.ai/api/v1/chat/completions",
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        headers={
            "Authorization": f"Bearer {_env('OPENROUTER_API_KEY')}",
            "HTTP-Referer": "https://claw247-trading.local",
            "X-Title": "claw247",
        },
    )
    if code != 200:
        return {"ok": False, "reason": f"http {code}", "raw": body[:400]}
    try:
        data = json.loads(body)
        text = data["choices"][0]["message"]["content"]
        return {"ok": True, "text": text, "model": model, "provider": "openrouter"}
    except Exception as exc:
        return {"ok": False, "reason": f"parse: {type(exc).__name__}", "raw": body[:400]}


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

def complete(
    prompt: str,
    *,
    provider: str = "auto",
    model: Optional[str] = None,
    max_tokens: int = 500,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """Send a prompt, return {ok, text, provider, model, latency_ms, ...}.

    Contract: never raises. On failure returns {ok=False, reason=<str>}.
    Every response is tagged commentary-only at the transport layer; callers
    wrapping this through claw.commentary get the non-authoritative stamp.
    """
    if not prompt or not str(prompt).strip():
        return {"ok": False, "reason": "empty_prompt"}
    chosen = _pick_provider(provider.lower())
    if not chosen:
        return {
            "ok": False,
            "reason": "no_provider_available",
            "available": available_providers(),
        }
    real_model = model or PROVIDER_MODELS[chosen]
    started = time.time()
    handler = {
        "openai":     _call_openai,
        "anthropic":  _call_anthropic,
        "gemini":     _call_gemini,
        "mistral":    _call_mistral,
        "openrouter": _call_openrouter,
    }[chosen]
    out = handler(prompt, model=real_model, max_tokens=max_tokens, temperature=temperature)
    out["latency_ms"] = int((time.time() - started) * 1000)
    out.setdefault("provider", chosen)
    out.setdefault("model", real_model)
    out["source"] = "claw.llm"
    out["authoritative"] = False
    return out
