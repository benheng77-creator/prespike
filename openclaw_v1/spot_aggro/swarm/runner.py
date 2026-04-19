"""
5-LLM Research Swarm Runner — permanent spot_aggro subsystem.

3 layers:
  Heavy   (4x/day)   — full universe, deep re-ranking
  Standard (10min)   — top 10, re-score + buy/watch/skip refresh
  Fast    (2min)     — top 5 candidates, final entry validation

SPOT_AGGRO ONLY. Zero apex_omega contamination.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import Any, Optional

from .models import CoinIntel, SwarmState
from . import prompts as swarm_prompts
from shared.persistence import state as persist

log = logging.getLogger("spot_aggro.swarm")

# ---------------------------------------------------------------------------
# Module state (thread-safe)
# ---------------------------------------------------------------------------

_state = SwarmState()
_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()

# Default intervals (overridden by config)
HEAVY_INTERVAL_S = 21600    # 6 hours
STANDARD_INTERVAL_S = 600   # 10 minutes
FAST_INTERVAL_S = 120       # 2 minutes
FAST_WATCHLIST_SIZE = 5
STANDARD_UNIVERSE_SIZE = 10
PER_CALL_TIMEOUT_S = 15

# Agent config (provider + model)
AGENTS = [
    {"role": "structure_analyst",  "provider": "anthropic",  "model": "claude-haiku-4-5"},
    {"role": "quant_analyst",      "provider": "openai",     "model": "gpt-4o-mini"},
    {"role": "liquidity_analyst",  "provider": "gemini",     "model": "gemini-2.5-flash"},
    {"role": "regime_analyst",     "provider": "openrouter", "model": "deepseek/deepseek-chat-v3"},
    {"role": "adjudicator",        "provider": "mistral",    "model": "mistral-small-latest"},
]


def get_swarm_state() -> SwarmState:
    with _lock:
        return _state


def get_coin_intel(symbol: str) -> Optional[CoinIntel]:
    with _lock:
        return _state.coin_intels.get(symbol)


def _set_coin_intel(ci: CoinIntel) -> None:
    with _lock:
        _state.coin_intels[ci.symbol] = ci
        _state.total_cost_usd += ci.cost_usd


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def _build_context(coin: dict[str, Any], mio: Any, layer: str,
                   has_position: bool = False) -> str:
    comps = coin.get("components", {})
    return swarm_prompts.COIN_CONTEXT.format(
        symbol=coin.get("symbol", "?"),
        price=coin.get("price", 0),
        spi=coin.get("spi", 0),
        spi_fz=comps.get("fz", 0), spi_oi=comps.get("oi", 0),
        spi_div=comps.get("div", 0), spi_liq=comps.get("liq", 0),
        funding_z=coin.get("funding_z", 0),
        funding_rate=coin.get("funding_rate", 0),
        sigma_30d=coin.get("sigma_30d", 0),
        oi_change=coin.get("oi_change", 0),
        ret_7d=coin.get("ret_7d", 0),
        depth_usd=coin.get("depth_usd", 0),
        spread_bp=coin.get("spread_bp", 0),
        composite=coin.get("composite", 0),
        tier=coin.get("tier", "?"),
        regime=getattr(mio, "regime", "UNKNOWN"),
        squeeze=getattr(mio, "squeeze_timing_window", "NONE"),
        edge=getattr(mio, "edge_status", "HEALTHY"),
        has_position="YES" if has_position else "NO",
        layer=layer,
    )


# ---------------------------------------------------------------------------
# Parse helper
# ---------------------------------------------------------------------------

def _parse_json(raw: str) -> Optional[dict]:
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
# Single-coin analysis (5 agents)
# ---------------------------------------------------------------------------

async def run_coin_analysis(
    coin: dict[str, Any],
    mio: Any,
    layer: str,
    has_position: bool = False,
) -> CoinIntel:
    """Run all 5 agents on one coin. Returns CoinIntel."""
    from shared.llm.consensus import _call_member

    ctx = _build_context(coin, mio, layer, has_position)
    sym = coin.get("symbol", "?")
    total_cost = 0.0
    agents_ok = 0

    # --- Phase 1: 4 analysts in parallel ---
    analyst_prompts = {
        "structure_analyst": swarm_prompts.STRUCTURE_ANALYST.replace("{context}", ctx),
        "quant_analyst": swarm_prompts.QUANT_ANALYST.replace("{context}", ctx),
        "liquidity_analyst": swarm_prompts.LIQUIDITY_ANALYST.replace("{context}", ctx),
        "regime_analyst": swarm_prompts.REGIME_ANALYST.replace("{context}", ctx),
    }

    async def _call_agent(role: str, prompt: str) -> tuple[str, Optional[dict], float]:
        agent = next((a for a in AGENTS if a["role"] == role), AGENTS[0])
        try:
            text, cost = await asyncio.wait_for(
                _call_member(role=f"swarm_{role}", provider=agent["provider"],
                             model=agent["model"], prompt=prompt),
                timeout=PER_CALL_TIMEOUT_S,
            )
            persist.log_llm_cost(
                symbol=sym, role=f"swarm_{role}", provider=agent["provider"],
                model=agent["model"], cost_usd=cost, latency_ms=0, ok=True,
            )
            return role, _parse_json(text), cost
        except Exception as exc:
            log.debug("swarm %s %s failed: %s", role, sym, exc)
            return role, None, 0.0

    results = await asyncio.gather(
        _call_agent("structure_analyst", analyst_prompts["structure_analyst"]),
        _call_agent("quant_analyst", analyst_prompts["quant_analyst"]),
        _call_agent("liquidity_analyst", analyst_prompts["liquidity_analyst"]),
        _call_agent("regime_analyst", analyst_prompts["regime_analyst"]),
    )

    scores = {}
    signals = {}
    for role, parsed, cost in results:
        total_cost += cost
        if parsed:
            agents_ok += 1
            scores[role] = float(parsed.get("score", 0.5))
            signals[role] = parsed.get("key_signal", "")
        else:
            scores[role] = 0.5
            signals[role] = "parse_failed"

    # --- Phase 2: Adjudicator ---
    adj_agent = next((a for a in AGENTS if a["role"] == "adjudicator"), AGENTS[-1])
    adj_prompt = swarm_prompts.ADJUDICATOR.format(
        context=ctx,
        structure_score=scores.get("structure_analyst", 0.5),
        structure_signal=signals.get("structure_analyst", ""),
        quant_score=scores.get("quant_analyst", 0.5),
        quant_signal=signals.get("quant_analyst", ""),
        liquidity_score=scores.get("liquidity_analyst", 0.5),
        liquidity_signal=signals.get("liquidity_analyst", ""),
        regime_score=scores.get("regime_analyst", 0.5),
        regime_signal=signals.get("regime_analyst", ""),
    )
    try:
        adj_text, adj_cost = await asyncio.wait_for(
            _call_member(role="swarm_adjudicator", provider=adj_agent["provider"],
                         model=adj_agent["model"], prompt=adj_prompt),
            timeout=PER_CALL_TIMEOUT_S,
        )
        total_cost += adj_cost
        adj = _parse_json(adj_text)
        persist.log_llm_cost(
            symbol=sym, role="swarm_adjudicator", provider=adj_agent["provider"],
            model=adj_agent["model"], cost_usd=adj_cost, latency_ms=0, ok=bool(adj),
        )
    except Exception:
        adj = None

    if adj:
        agents_ok += 1

    # Build CoinIntel
    ci = CoinIntel(
        symbol=sym,
        tradable_state=adj.get("tradable_state", "WATCH") if adj else "WATCH",
        buy_confidence=float(adj.get("buy_confidence", 0.5)) if adj else 0.5,
        structure_score=scores.get("structure_analyst", 0.5),
        quant_score=scores.get("quant_analyst", 0.5),
        liquidity_score=scores.get("liquidity_analyst", 0.5),
        regime_score=scores.get("regime_analyst", 0.5),
        final_action=adj.get("final_action", "WATCH") if adj else "WATCH",
        final_reason=adj.get("rationale", "") if adj else "adjudicator failed",
        sizing_hint=max(0.5, min(1.5, float(adj.get("sizing_hint", 1.0)))) if adj else 1.0,
        exit_urgency=max(0.0, min(1.0, float(adj.get("exit_urgency", 0.0)))) if adj else 0.0,
        false_positive_risk=float(adj.get("false_positive_risk", 0.5)) if adj else 0.5,
        timestamp=time.time(),
        layer=layer,
        cost_usd=total_cost,
        agents_ok=agents_ok,
        agents_total=5,
    )

    _set_coin_intel(ci)
    return ci


# ---------------------------------------------------------------------------
# Layer runners
# ---------------------------------------------------------------------------

async def _run_heavy(engine_ref: Any) -> None:
    """Full universe deep analysis. 4x/day."""
    from ..research.runner import get_mio
    mio = get_mio()
    rankings = getattr(engine_ref, '_rank_cache', []) or []
    positions = engine_ref.state.positions

    log.info("swarm HEAVY: analyzing %d coins", len(rankings))
    for i, coin in enumerate(rankings):
        sym = coin.get("symbol", "?")
        ci = await run_coin_analysis(coin, mio, "heavy", has_position=sym in positions)
        ci.rank = i + 1

    # Update fast watchlist (top 5 by buy_confidence)
    with _lock:
        _state.last_heavy_ts = time.time()
        _state.cycle_counts["heavy"] += 1
        ranked = sorted(_state.coin_intels.values(), key=lambda c: -c.buy_confidence)
        _state.fast_watchlist = [c.symbol for c in ranked[:FAST_WATCHLIST_SIZE]]

    log.info("swarm HEAVY done: fast_watchlist=%s", _state.fast_watchlist)


async def _run_standard(engine_ref: Any) -> None:
    """Top 10 re-scoring. Every 10 min."""
    from ..research.runner import get_mio
    mio = get_mio()
    rankings = getattr(engine_ref, '_rank_cache', []) or []
    positions = engine_ref.state.positions
    coins = rankings[:STANDARD_UNIVERSE_SIZE]

    log.info("swarm STANDARD: scoring %d coins", len(coins))
    for i, coin in enumerate(coins):
        sym = coin.get("symbol", "?")
        ci = await run_coin_analysis(coin, mio, "standard", has_position=sym in positions)
        ci.rank = i + 1

    with _lock:
        _state.last_standard_ts = time.time()
        _state.cycle_counts["standard"] += 1
        # Refresh fast watchlist
        ranked = sorted(_state.coin_intels.values(), key=lambda c: -c.buy_confidence)
        _state.fast_watchlist = [c.symbol for c in ranked[:FAST_WATCHLIST_SIZE]]


async def _run_fast(engine_ref: Any) -> None:
    """Top 5 candidates fast validation. Every 2 min."""
    from ..research.runner import get_mio
    mio = get_mio()
    rankings = getattr(engine_ref, '_rank_cache', []) or []
    positions = engine_ref.state.positions
    rmap = {r["symbol"]: r for r in rankings}

    with _lock:
        watchlist = list(_state.fast_watchlist)

    # Also include currently held positions
    for sym in positions:
        if sym not in watchlist:
            watchlist.append(sym)
    watchlist = watchlist[:FAST_WATCHLIST_SIZE + 3]  # cap

    coins = [rmap[s] for s in watchlist if s in rmap]
    if not coins:
        return

    log.debug("swarm FAST: %d coins %s", len(coins), [c["symbol"] for c in coins])
    for coin in coins:
        sym = coin.get("symbol", "?")
        await run_coin_analysis(coin, mio, "fast", has_position=sym in positions)

    with _lock:
        _state.last_fast_ts = time.time()
        _state.cycle_counts["fast"] += 1


# ---------------------------------------------------------------------------
# Scheduler daemon thread
# ---------------------------------------------------------------------------

def start(engine_ref: Any) -> None:
    """Launch swarm daemon. Called from engine.run_forever()."""
    global _thread
    if _thread and _thread.is_alive():
        return

    # Load config
    try:
        from shared.config import load as load_cfg
        cfg = load_cfg(engine="spot_aggro").get("swarm", {})
        if not cfg.get("enabled", True):
            log.info("swarm disabled by config")
            with _lock:
                _state.enabled = False
            return

        global HEAVY_INTERVAL_S, STANDARD_INTERVAL_S, FAST_INTERVAL_S
        global FAST_WATCHLIST_SIZE, STANDARD_UNIVERSE_SIZE, PER_CALL_TIMEOUT_S
        HEAVY_INTERVAL_S = cfg.get("heavy_interval_s", 21600)
        STANDARD_INTERVAL_S = cfg.get("standard_interval_s", 600)
        FAST_INTERVAL_S = cfg.get("fast_interval_s", 120)
        FAST_WATCHLIST_SIZE = cfg.get("fast_watchlist_size", 5)
        STANDARD_UNIVERSE_SIZE = cfg.get("standard_universe_size", 10)
        PER_CALL_TIMEOUT_S = cfg.get("per_call_timeout_s", 15)
    except Exception:
        pass

    _stop.clear()

    # Initialise health fields so dashboard can distinguish warming_up from
    # crashed even before the first heartbeat fires.
    with _lock:
        h = _state.health
        h.started_at = time.time()
        h.last_heartbeat_ts = h.started_at
        h.last_cycle_ts = 0.0
        h.last_error = None
        h.last_error_ts = 0.0
        h.consecutive_errors = 0
        h.alive = True
        # Warmup: short enough to detect a real crash quickly, long enough to
        # let the engine populate rankings before the first fast-layer poll.
        h.warmup_s = 5.0
        # Stall threshold: outer loop heartbeats every 10s; allow 4× that.
        h.stall_after_s = 40.0

    def _loop():
        log.info("swarm started (heavy=%ds standard=%ds fast=%ds)",
                 HEAVY_INTERVAL_S, STANDARD_INTERVAL_S, FAST_INTERVAL_S)
        try:
            # Brief warmup so engine.rankings is populated before first poll.
            # Short enough that 'warming_up' is distinguishable from a hang.
            time.sleep(5)

            while not _stop.is_set():
                # Heartbeat every iteration — separates thread pulse from
                # work pulse. A stalled thread stops updating this even if
                # no cycle is due yet.
                with _lock:
                    _state.health.last_heartbeat_ts = time.time()

                now = time.time()
                cycle_ran = False
                layer = None
                try:
                    if now - _state.last_heavy_ts >= HEAVY_INTERVAL_S:
                        layer = "heavy"
                        asyncio.run(_run_heavy(engine_ref))
                        cycle_ran = True
                    elif now - _state.last_standard_ts >= STANDARD_INTERVAL_S:
                        layer = "standard"
                        asyncio.run(_run_standard(engine_ref))
                        cycle_ran = True
                    elif now - _state.last_fast_ts >= FAST_INTERVAL_S:
                        layer = "fast"
                        asyncio.run(_run_fast(engine_ref))
                        cycle_ran = True
                except Exception as e:
                    # Log everything caught — never silently drop.
                    log.exception("swarm %s cycle failed", layer or "scheduler")
                    with _lock:
                        _state.health.consecutive_errors += 1
                        _state.health.last_error = (
                            f"{layer or 'scheduler'}: {type(e).__name__}: {e}"
                        )[:300]
                        _state.health.last_error_ts = time.time()

                if cycle_ran:
                    with _lock:
                        _state.health.last_cycle_ts = time.time()
                        _state.health.consecutive_errors = 0
                        _state.health.last_error = None

                # Sleep 10s between checks, broken into 1s slices so stop()
                # is responsive.
                for _ in range(10):
                    if _stop.is_set():
                        return
                    time.sleep(1)
        finally:
            # Thread is exiting — flip alive flag so dashboard sees 'crashed'
            # instead of a stale 'running'. Runs on normal stop and on
            # uncaught exception escape.
            with _lock:
                _state.health.alive = False
            log.info("swarm stopped")

    _thread = threading.Thread(target=_loop, name="spot_aggro_swarm", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
