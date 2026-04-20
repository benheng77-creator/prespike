"""
5-LLM Bayesian consensus engine (APEX-Ω §2).

Calls all 5 cheap-tier members in parallel. Bayesian posterior updates as
each completes. After 3rd member, checks KL-divergence; if stable, stops
early. If `conflict_score > trigger_conflict`, escalates to Opus veto.

Every call's cost + latency logged to persistence.log_llm_cost. Every
consensus decision logged to persistence.log_consensus.

Output:
    ConsensusResult(
        consensus=0..1,     # Bayesian posterior
        conflict=0..inf,    # dispersion / |mean|
        vetoed: bool,       # Opus said "block"
        members_called: int,
        kl_stop_at: Optional[int],   # index where we stopped
        per_member: list[MemberResult],
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from ..config import load as load_cfg
from ..persistence import state as persist


log = logging.getLogger("ops.llm.consensus")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class MemberResult:
    role: str
    provider: str
    model: str
    score: float = 0.0            # [-1, 1]: -1 veto, +1 full support
    confidence: float = 0.0       # [0, 1]
    rationale: str = ""
    cost_usd: float = 0.0
    latency_ms: int = 0
    ok: bool = False
    error: Optional[str] = None


@dataclass
class ConsensusResult:
    consensus: float = 0.5
    conflict: float = 0.0
    vetoed: bool = False
    members_called: int = 0
    kl_stop_at: Optional[int] = None
    per_member: list[MemberResult] = field(default_factory=list)
    veto_member: Optional[MemberResult] = None
    symbol: str = ""

    def size_multiplier(self, gain: float = 8.0, mid: float = 0.55) -> float:
        """σ(gain · (consensus − mid)) — the position-size multiplier."""
        x = gain * (self.consensus - mid)
        return 1.0 / (1.0 + math.exp(-x))


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

_ROLE_PROMPTS = {
    "pattern": (
        "You are a PATTERN VALIDATOR for a DELTA-NEUTRAL funding harvest bot. "
        "We simultaneously hold SPOT + opposite PERP so coin direction is irrelevant. "
        "Only question: is this funding rate {funding:.5f} on {symbol} (z={z:.2f} vs "
        "30d μ={mu_30d:.5f} σ={sigma_30d:.5f}) a persistent pattern we can harvest "
        "for the next 8-24h, or is it a one-off spike likely to flip? "
        "Score +1 if persistent, -1 if likely to flip within 8h."
    ),
    "macro": (
        "You are a MACRO analyst for a DELTA-NEUTRAL funding harvest. We hold both "
        "spot and perp so price direction does NOT matter. Funding={funding:.5f} on "
        "{symbol} (z={z:.2f}). Is there a macro event (FOMC, CPI, unlock, listing) "
        "in the next 24h that could cause extreme volatility and BLOW OUT the "
        "spot-perp basis? Score +1 if calm, -1 if event-risk is high."
    ),
    "sentiment": (
        "You are a SENTIMENT analyst for a DELTA-NEUTRAL funding harvest. Direction "
        "is irrelevant (we are hedged). Funding={funding:.5f} on {symbol} (z={z:.2f}). "
        "Question: are retail flows likely to SUSTAIN this funding rate for the next "
        "8h settlement cycle? Score +1 if flows sustain, -1 if sentiment is shifting."
    ),
    "quant_sanity": (
        "You are a QUANT SANITY checker. This is a delta-neutral funding harvest. "
        "EV per cycle = |funding| - cost. |funding|={abs_f:.6f}, cost≈0.002. "
        "EV = {abs_f:.6f} - 0.002 = net. Is EV positive? Is the funding rate z={z:.2f} "
        "high enough relative to execution cost to justify entry? "
        "Score +1 if EV positive and z>1.5, -1 if EV negative or marginal."
    ),
    "outlier": (
        "You are an OUTLIER detector for a DELTA-NEUTRAL strategy. We do NOT care "
        "if {symbol} goes up or down — we hold both sides. Funding={funding:.5f} "
        "(z={z:.2f}), book depth={depth_usd:.0f} USD. "
        "Only flag manipulation if someone is ARTIFICIALLY INFLATING the funding rate "
        "itself (not the price). Is this funding rate organic? "
        "Score +1 if organic, 0 if unsure, -1 only if clear evidence of funding manipulation."
    ),
}

_SCHEMA_SUFFIX = (
    "\n\nRespond ONLY with a compact JSON object — no code fences, no prose outside. Schema:\n"
    '{"score": <-1..+1 float>, "confidence": <0..1 float>, "rationale": "<≤40 words>"}\n'
    'score=+1 → strong go, 0 → neutral, -1 → strong block. confidence=how sure you are.'
)

_VETO_PROMPT = (
    "You are the VETO AUTHORITY. Five analysts examined {symbol} and reached consensus "
    "but with high disagreement (conflict={conflict:.2f}). Here are their rationales:\n"
    "{rationales}\n\n"
    "Respond ONLY with a compact JSON: "
    '{{"veto": <true|false>, "confidence": <0..1>, "rationale": "<30 words max>"}}. '
    "Veto=true means BLOCK this trade."
)


def build_ensemble_prompts(symbol: str, ctx: dict[str, Any]) -> dict[str, str]:
    """Return {role: full_prompt} for each member."""
    out = {}
    for role, tmpl in _ROLE_PROMPTS.items():
        out[role] = tmpl.format(
            symbol=symbol,
            funding=ctx.get("funding", 0.0),
            abs_f=abs(ctx.get("funding", 0.0)),
            mu_30d=ctx.get("mu_30d", 0.0),
            sigma_30d=ctx.get("sigma_30d", 0.0),
            z=ctx.get("z", 0.0),
            depth_usd=ctx.get("depth_usd", 0.0),
        ) + _SCHEMA_SUFFIX
    return out


# ---------------------------------------------------------------------------
# JSON parsing — robust to prose / fences
# ---------------------------------------------------------------------------

_JSON_BLOCK = re.compile(r"\{[^{}]*\}", re.DOTALL)


def parse_member_output(text: str) -> tuple[Optional[float], Optional[float], str]:
    """Return (score, confidence, rationale). Any None → parse failed."""
    if not text:
        return None, None, ""
    s = text.strip().strip("`")
    for prefix in ("json\n", "JSON\n", "json ", "JSON "):
        if s.startswith(prefix):
            s = s[len(prefix):]
    # Try direct JSON first
    candidates = [s]
    # Then any {...} blocks
    for m in _JSON_BLOCK.finditer(s):
        candidates.append(m.group(0))
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if not isinstance(obj, dict):
                continue
            sc = obj.get("score")
            cf = obj.get("confidence")
            rt = obj.get("rationale") or ""
            if sc is None or cf is None:
                continue
            sc = max(-1.0, min(1.0, float(sc)))
            cf = max(0.0, min(1.0, float(cf)))
            return sc, cf, str(rt)[:240]
        except Exception:
            continue
    return None, None, ""


# ---------------------------------------------------------------------------
# Bayesian + KL helpers
# ---------------------------------------------------------------------------

def bayes_update(prior: float, score: float, confidence: float) -> float:
    """One-step Bayesian posterior update; returns clipped in (ε, 1-ε).

    Confidence is CAPPED at 0.70 before the likelihood computation so that
    no single highly-confident member can dominate the entire posterior.
    Without this cap, a single member at (-0.90, 0.95) tanks the posterior
    to ~0.07 and 4 positive members can't recover it.
    """
    eps = 1e-6
    conf_capped = min(confidence, 0.70)
    likelihood_pos = 0.5 + 0.5 * score * conf_capped
    likelihood_neg = 1.0 - likelihood_pos
    numer = prior * likelihood_pos
    denom = numer + (1.0 - prior) * likelihood_neg
    post = numer / max(denom, eps)
    return max(eps, min(1.0 - eps, post))


def kl_binary(p: float, q: float) -> float:
    """KL(Bern(p) || Bern(q)). Both in (0,1)."""
    eps = 1e-9
    p = min(1 - eps, max(eps, p))
    q = min(1 - eps, max(eps, q))
    return p * math.log(p / q) + (1 - p) * math.log((1 - p) / (1 - q))


def compute_conflict(scores: list[float]) -> float:
    """Raw standard deviation in [0, 1]. Score range is [-1, +1] so σ ≤ 1.

    The previous formulation dispersion/(|mean|+0.01) exploded toward infinity
    whenever the mean crossed zero — e.g. one member at -0.9 and another at
    +0.7 produced conflict≈9 on a perfectly reasonable disagreement. Using
    raw σ makes the threshold interpretable: 0.7 means the members disagree
    by ~±0.7 on average, which is the real disagreement we want to gate on.
    """
    if not scores:
        return 0.0
    mean = sum(scores) / len(scores)
    var = sum((s - mean) ** 2 for s in scores) / len(scores)
    return var ** 0.5


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def run_consensus(*, symbol: str, ctx: dict[str, Any],
                        member_count: int = 5) -> ConsensusResult:
    """Run LLM consensus on this symbol. member_count controls tier depth.

    Tier A+/A: member_count=5 (full ensemble)
    Tier B:    member_count=3 (pattern + macro + sentiment)
    Tier C:    member_count=1 (pattern only — sanity check)
    """
    cfg = load_cfg()["llm"]
    prompts = build_ensemble_prompts(symbol, ctx)
    all_members = cfg["members"]
    members_cfg = all_members[:min(member_count, len(all_members))]
    kl_stop = float(cfg["kl_early_stop_threshold"])
    per_timeout = float(cfg["per_call_timeout_s"])

    async def _one(m: dict[str, Any]) -> MemberResult:
        role = m["role"]
        mr = MemberResult(role=role, provider=m["provider"], model=m["model"])
        started = time.time()
        try:
            text, cost = await asyncio.wait_for(
                _call_member(role=role, provider=m["provider"], model=m["model"],
                             prompt=prompts[role]),
                timeout=per_timeout,
            )
            mr.latency_ms = int((time.time() - started) * 1000)
            mr.cost_usd = cost
            score, conf, rationale = parse_member_output(text)
            if score is None:
                mr.ok = False
                mr.error = "parse_failed"
                mr.rationale = f"raw: {text[:80]!r}"
            else:
                mr.ok = True
                mr.score = score
                mr.confidence = conf or 0.0
                mr.rationale = rationale
        except asyncio.TimeoutError:
            mr.latency_ms = int((time.time() - started) * 1000)
            mr.ok = False
            mr.error = "timeout"
        except Exception as exc:
            mr.latency_ms = int((time.time() - started) * 1000)
            mr.ok = False
            mr.error = f"{type(exc).__name__}: {str(exc)[:140]}"
        persist.log_llm_cost(
            symbol=symbol, role=role, provider=mr.provider, model=mr.model,
            cost_usd=mr.cost_usd, latency_ms=mr.latency_ms, ok=mr.ok,
            error=mr.error,
        )
        return mr

    tasks = [asyncio.create_task(_one(m)) for m in members_cfg]
    per_member: list[MemberResult] = []
    posterior = 0.5
    prior_at_stop: Optional[float] = None
    kl_stop_at: Optional[int] = None

    for i, fut in enumerate(asyncio.as_completed(tasks)):
        mr = await fut
        per_member.append(mr)
        if mr.ok:
            prior_before = posterior
            posterior = bayes_update(posterior, mr.score, mr.confidence)
            # After 3rd informative update, check KL; if stable, cancel remaining
            if i >= 2 and prior_at_stop is not None:
                kl = kl_binary(posterior, prior_at_stop)
                if kl < kl_stop:
                    kl_stop_at = i + 1
                    break
            if i >= 1:
                prior_at_stop = prior_before
    # Cancel any still-running tasks after KL early-stop
    if kl_stop_at is not None:
        for t in tasks:
            if not t.done():
                t.cancel()
        # Collect cancellations silently
        await asyncio.gather(*tasks, return_exceptions=True)

    scores = [m.score for m in per_member if m.ok]
    conflict = compute_conflict(scores)

    # Escalate to Opus veto on high conflict
    vetoed = False
    veto_mr: Optional[MemberResult] = None
    if conflict > float(cfg["veto"]["trigger_conflict"]) and len(members_cfg) >= 3:
        veto_mr = await _run_opus_veto(
            symbol=symbol, conflict=conflict, per_member=per_member,
        )
        if veto_mr and veto_mr.ok and veto_mr.score < -0.1:
            vetoed = True

    result = ConsensusResult(
        consensus=posterior if any(m.ok for m in per_member) else 0.5,
        conflict=conflict,
        vetoed=vetoed,
        members_called=len(per_member),
        kl_stop_at=kl_stop_at,
        per_member=per_member,
        veto_member=veto_mr,
        symbol=symbol,
    )
    persist.log_consensus(
        symbol=symbol,
        consensus_score=result.consensus, conflict_score=result.conflict,
        vetoed=result.vetoed, members_called=result.members_called,
        kl_stop_at=result.kl_stop_at,
        payload={
            "per_member": [
                {"role": m.role, "ok": m.ok, "score": m.score,
                 "confidence": m.confidence, "error": m.error,
                 "cost_usd": m.cost_usd, "latency_ms": m.latency_ms}
                for m in per_member
            ],
            "ctx": ctx,
        },
    )
    return result


# ---------------------------------------------------------------------------
# LLM dispatch — reuses claw.llm.complete with forced model override
# ---------------------------------------------------------------------------

async def _call_member(*, role: str, provider: str, model: str,
                       prompt: str) -> tuple[str, float]:
    """Return (text, cost_usd). Runs sync claw.llm.complete in thread."""
    def _sync():
        from claw import llm as claw_llm
        out = claw_llm.complete(
            prompt, provider=provider, model=model,
            max_tokens=600, temperature=0.1,
        )
        return out
    out = await asyncio.to_thread(_sync)
    if not out.get("ok"):
        raise RuntimeError(out.get("reason") or "llm_unavailable")
    return (out.get("text") or ""), _rough_cost_usd(provider, model,
                                                    len(prompt), len(out.get("text") or ""))


async def _run_opus_veto(*, symbol: str, conflict: float,
                         per_member: list[MemberResult]) -> Optional[MemberResult]:
    cfg = load_cfg()["llm"]["veto"]
    rationales = "\n".join(
        f"- {m.role} (score={m.score:+.2f} conf={m.confidence:.2f}): {m.rationale}"
        for m in per_member if m.ok
    )
    prompt = _VETO_PROMPT.format(symbol=symbol, conflict=conflict,
                                 rationales=rationales or "(no member rationales)")
    mr = MemberResult(role="veto", provider=cfg["provider"], model=cfg["model"])
    started = time.time()
    try:
        text, cost = await asyncio.wait_for(
            _call_member(role="veto", provider=cfg["provider"], model=cfg["model"],
                         prompt=prompt),
            timeout=load_cfg()["llm"]["per_call_timeout_s"],
        )
        mr.latency_ms = int((time.time() - started) * 1000)
        mr.cost_usd = cost
        try:
            obj = json.loads(text)
            veto = bool(obj.get("veto"))
            conf = float(obj.get("confidence") or 0)
            rat  = str(obj.get("rationale") or "")[:240]
            mr.ok = True
            mr.score = -1.0 if veto else +1.0
            mr.confidence = conf
            mr.rationale = rat
        except Exception:
            mr.ok = False
            mr.error = "parse_failed"
            mr.rationale = f"raw: {text[:80]!r}"
    except Exception as exc:
        mr.ok = False
        mr.error = f"{type(exc).__name__}: {str(exc)[:120]}"
    persist.log_llm_cost(
        symbol=symbol, role="veto", provider=mr.provider, model=mr.model,
        cost_usd=mr.cost_usd, latency_ms=mr.latency_ms, ok=mr.ok, error=mr.error,
    )
    return mr


# Phase 11n-9-bb — real 2026 list pricing per 1M tokens (input/output
# blended). Prior table undercounted by ~400x — operator's Anthropic
# billing confirmed haiku at ~$1-1.5/MTok and opus at ~$25/MTok.
_PRICES = {
    ("anthropic", "claude-haiku-4-5"):            1.50,
    ("openai",    "gpt-4o-mini"):                 0.30,
    ("gemini",    "gemini-2.5-flash"):            0.20,
    ("openrouter","deepseek/deepseek-chat-v3"):   0.20,
    ("mistral",   "mistral-small-latest"):        0.30,
    ("anthropic", "claude-opus-4-6"):             25.00,
}


def _rough_cost_usd(provider: str, model: str, prompt_chars: int, out_chars: int) -> float:
    """Estimate cost in USD. Price table is USD per 1M tokens;
    tokens = chars / 4 (standard tokenizer heuristic)."""
    price_per_mtok = _PRICES.get((provider, model), 0.50)
    tokens = (prompt_chars + out_chars) / 4.0
    return price_per_mtok * tokens / 1_000_000.0
