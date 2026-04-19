"""
30-minute research cycle runner.
Fires 5 LLMs in parallel, builds MarketIntelligence, applies to engine.
SPEC: APEX_OMEGA_SPOT_30MIN_RESEARCH.py — strictly complied.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import threading
from typing import Any, Optional

from .mio import MarketIntelligence
from .prompts import (
    RESEARCH_CONTEXT, RESEARCH_HAIKU_REGIME, RESEARCH_GPT_EVENTS,
    RESEARCH_GEMINI_FUNDING, RESEARCH_DEEPSEEK_AUDIT, RESEARCH_MISTRAL_UNIVERSE,
)
from shared.persistence import state as persist


log = logging.getLogger("apex.spot_aggro.research")

INTERVAL_S = 1800  # 30 minutes

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_latest_mio: Optional[MarketIntelligence] = None
_mio_lock = threading.Lock()


def get_mio() -> MarketIntelligence:
    with _mio_lock:
        return _latest_mio or MarketIntelligence()


def _set_mio(mio: MarketIntelligence) -> None:
    global _latest_mio
    with _mio_lock:
        _latest_mio = mio


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def _build_context(engine_status: dict[str, Any], cycle_number: int) -> str:
    rankings = engine_status.get("rankings", [])
    asset_lines = []
    for r in rankings[:10]:
        asset_lines.append(
            f"  {r.get('symbol','?'):12} SPI={r.get('spi',0):.3f} "
            f"funding={r.get('funding_rate',0):+.6f}% z={r.get('funding_z',0):.2f}"
        )
    funding_lines = []
    for r in rankings:
        funding_lines.append(f"  {r.get('symbol','?'):12} {r.get('funding_rate',0):+.6f}%  z={r.get('funding_z',0):.2f}")

    return RESEARCH_CONTEXT.format(
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        cycle_number=cycle_number,
        asset_table="\n".join(asset_lines) or "  (no data)",
        capital=engine_status.get("capital_usd", 0),
        open_count=len(engine_status.get("positions", {})),
        dd_pct=engine_status.get("dd_pct", 0),
        trades_2h=engine_status.get("trades_2h", 0),
        wins_2h=engine_status.get("wins_2h", 0),
        losses_2h=engine_status.get("losses_2h", 0),
        wr_2h=engine_status.get("wr_2h", 0),
        pnl_2h=engine_status.get("pnl_2h", 0),
        best_trade=engine_status.get("best_trade", 0),
        worst_trade=engine_status.get("worst_trade", 0),
        funding_table="\n".join(funding_lines) or "  (no data)",
        trade_log=engine_status.get("trade_log_text", "  (no trades)"),
    )


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------

def _parse(raw: str) -> Optional[dict]:
    try:
        clean = raw.strip()
        clean = re.sub(r"^```json\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean)
        m = re.search(r"\{.*\}", clean, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Run one cycle
# ---------------------------------------------------------------------------

async def run_cycle(engine_status: dict[str, Any], cycle_number: int) -> MarketIntelligence:
    ctx = _build_context(engine_status, cycle_number)

    async def _call(role: str, provider: str, model: str, prompt: str) -> Optional[dict]:
        from shared.llm.consensus import _call_member
        try:
            text, cost = await asyncio.wait_for(
                _call_member(role=role, provider=provider, model=model, prompt=prompt),
                timeout=15,
            )
            persist.log_llm_cost(
                symbol=None, role=f"research_{role}", provider=provider,
                model=model, cost_usd=cost, latency_ms=0, ok=True,
            )
            return _parse(text)
        except Exception as exc:
            log.debug("research %s failed: %s", role, exc)
            return None

    # Fire all 5 in parallel per spec
    results = await asyncio.gather(
        _call("regime", "anthropic", "claude-haiku-4-5",
              RESEARCH_HAIKU_REGIME.replace("{context}", ctx)),
        _call("events", "openai", "gpt-4o-mini",
              RESEARCH_GPT_EVENTS.replace("{context}", ctx)),
        _call("funding", "gemini", "gemini-2.5-flash",
              RESEARCH_GEMINI_FUNDING.replace("{context}", ctx)),
        _call("audit", "openrouter", "deepseek/deepseek-chat-v3",
              RESEARCH_DEEPSEEK_AUDIT.replace("{context}", ctx)),
        _call("universe", "mistral", "mistral-small-latest",
              RESEARCH_MISTRAL_UNIVERSE.replace("{context}", ctx)),
        return_exceptions=True,
    )

    regime, events, funding, audit, universe = [
        r if isinstance(r, dict) else None for r in results
    ]

    mio = MarketIntelligence()
    mio.timestamp = time.time()
    mio.cycle_number = cycle_number

    if regime:
        mio.regime = regime.get("regime", "UNKNOWN")
        mio.regime_confidence = float(regime.get("confidence", 0.5))
        mio.spi_threshold_adj = float(regime.get("spi_threshold_adj", 0))
        mio.tp_multiplier = float(regime.get("tp_multiplier", 1.0))
        mio.sl_multiplier = float(regime.get("sl_multiplier", 1.0))

    if events:
        mio.events_next_24h = [e.get("name", "") for e in events.get("events", [])]
        mio.event_impact_score = float(events.get("event_impact_score", 0))
        mio.event_volatility_boost = float(events.get("event_volatility_boost", 1.0))
        mio.blitz_readiness = float(events.get("blitz_readiness", 0))

    if funding:
        mio.funding_direction_24h = funding.get("funding_direction_24h", "NEGATIVE")
        mio.funding_magnitude_pred = float(funding.get("funding_magnitude_pred", 0))
        mio.squeeze_timing_window = funding.get("squeeze_timing_window", "NONE")
        mio.spi_funding_weight_adj = float(funding.get("spi_funding_weight_adj", 0))

    if audit:
        mio.rolling_win_rate_2h = float(audit.get("rolling_win_rate_2h", 0.65))
        edge = audit.get("edge_status", "HEALTHY")
        # INSUFFICIENT_DATA = no trade history yet — treat as HEALTHY (no penalty)
        mio.edge_status = "HEALTHY" if edge == "INSUFFICIENT_DATA" else edge
        mio.risk_mult_adj = float(audit.get("risk_mult_adj", 0))
        mio.recommended_position_scale = float(audit.get("recommended_position_scale", 1.0))

    if universe:
        top6 = universe.get("top_6", [])
        mio.top_6_assets = [a["symbol"] for a in top6 if "symbol" in a]
        mio.asset_scores = {a["symbol"]: a.get("score", 0) for a in top6}
        mio.cohort_rotation_active = bool(universe.get("cohort_rotation_active"))
        bp = universe.get("best_cohort_pair")
        mio.best_cohort_pair = tuple(bp) if bp and len(bp) == 2 else None
        mio.universe_quality = universe.get("universe_quality", "NORMAL")

    _set_mio(mio)
    log.info("research cycle #%d: regime=%s edge=%s universe=%s squeeze=%s",
             cycle_number, mio.regime, mio.edge_status,
             mio.universe_quality, mio.squeeze_timing_window)

    # ALERT: notify when squeeze pressure detected
    if mio.regime in ("SQUEEZE_BUILDING", "CRISIS") or mio.squeeze_timing_window in ("IMMINENT", "NEAR"):
        from shared.notifications import router as notify
        notify.send(notify.NotifyEvent(
            event_type="engine.start",
            severity="warn",
            title=f"SQUEEZE DETECTED — {mio.regime}",
            body=(f"Timing: {mio.squeeze_timing_window} | "
                  f"Top: {', '.join(mio.top_6_assets[:3])} | "
                  f"SPI adj: {mio.spi_threshold_adj:+.2f}"),
        ))

    return mio


# ---------------------------------------------------------------------------
# apply_intelligence — spec function, NEVER MODIFY
# ---------------------------------------------------------------------------

def apply_intelligence(engine: Any, mio: MarketIntelligence) -> None:
    """Apply MIO to engine params. Called every heartbeat. Spec-compliant."""
    engine._effective_spi_min = max(0.40, min(0.85,
        0.65 + mio.spi_threshold_adj))
    engine._tp_regime_mult = max(0.3, min(2.5, mio.tp_multiplier))
    engine._sl_regime_mult = max(0.3, min(2.0, mio.sl_multiplier))
    engine._vol_boost = max(1.0, min(3.0, mio.event_volatility_boost))
    engine._blitz_hot = mio.blitz_readiness > 0.60
    engine._position_scale = max(0.3, min(1.5, mio.recommended_position_scale))
    engine._risk_mult_bonus = max(-0.20, min(0.10, mio.risk_mult_adj))


# ---------------------------------------------------------------------------
# Scheduler — daemon thread, 30-min cycle
# ---------------------------------------------------------------------------

def start(engine_ref: Any) -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    cycle_number = [0]

    def _loop():
        log.info("research cycle started (interval=%ds)", INTERVAL_S)
        import time as _t
        _t.sleep(30)  # let engine warm up
        while not _stop.is_set():
            cycle_number[0] += 1
            try:
                status = engine_ref.status()
                status["rankings"] = getattr(engine_ref, '_rank_cache', [])
                status["dd_pct"] = (
                    (engine_ref.state.peak_equity - engine_ref.state.current_equity)
                    / max(engine_ref.state.peak_equity, 1) * 100
                )
                status["trades_2h"] = 0
                status["wins_2h"] = 0
                status["losses_2h"] = 0
                status["wr_2h"] = 0
                status["pnl_2h"] = 0
                status["best_trade"] = 0
                status["worst_trade"] = 0
                status["trade_log_text"] = "  (see apex_trade_log)"
                mio = asyncio.run(run_cycle(status, cycle_number[0]))
                apply_intelligence(engine_ref, mio)
            except Exception:
                log.exception("research cycle crashed (recovering)")
            for _ in range(INTERVAL_S):
                if _stop.is_set():
                    return
                _t.sleep(1)

    _thread = threading.Thread(target=_loop, name="spot_aggro_research", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
