"""
APEX-Omega SPOT AGGRO v2.0 — Tiered Intraday Production Engine.

Spot-only, OKX EEA compliant, 24/7.
8-factor composite scoring → A+/A/B/C tier classification.
Per-tier consensus depth, sizing, TP/SL, and exits.
Target: 3–10 trades/day. Kill switch 8% DD.
BLITZ MODE = Tier A+ (SPI ≥ 0.85, composite ≥ 0.65).

Preserves: SPI formula, MIO research cycle, drawdown management.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from shared.adapters import OKXUnified, OKXError, OrderReceipt
from shared.config import load as _load_cfg
from shared.llm import consensus as llm_consensus
from shared.notifications import router as notify
from shared.persistence import state as persist
from shared.persistence import settings as app_settings
from . import spi as spi_mod
from . import prompts as spot_prompts
from . import coin_memory
from .scoring import (
    compute_composite_score, classify_tier, DailyFrequencyController,
    TierConfig, TIER_PARAMS,
)


def load_cfg():
    """Load spot_aggro-specific config. Isolated from apex_omega."""
    return _load_cfg(engine="spot_aggro")


# Phase 11f — logger namespace owned by spot_aggro, not apex. The previous
# "apex.spot_aggro" name caused log-aggregation tooling to group this
# engine under apex_omega by prefix — a classification error. No apex
# ownership exists for this engine at any level.
log = logging.getLogger("spot_aggro.engine")

# ---------------------------------------------------------------------------
# Config defaults (from spec §0)
# ---------------------------------------------------------------------------

WORKING_USD = 342.25
MAX_POSITIONS = 8
SPI_MIN = 0.65
FUNDING_Z_MAX = -1.0
CONSENSUS_MIN = 0.45
CONFLICT_MAX = 0.75
KILL_DD_PCT = 0.08
BLITZ_ALLOC = 0.60
BLITZ_SPI_MIN = 0.85
BLITZ_COOLDOWN_S = 14400
BLITZ_MAX_HOLD_S = 3600
BLITZ_DAILY_CAP = 3
DEPLOY_CEIL = 0.95
POLL_INTERVAL_S = 1


@dataclass
class Position:
    symbol: str
    side: str             # always "buy" for spot
    size_usd: float
    entry_price: float
    entry_spi: float
    entry_time: float
    tp: float             # fraction (e.g., 0.032)
    sl: float             # negative fraction (e.g., -0.0115)
    max_ret: float = 0.0
    is_blitz: bool = False
    module: str = "M1_squeeze"
    tier: str = "A"                   # v2: A+/A/B/C
    composite_score: float = 0.0      # v2: composite at entry
    trail_activate: float = 0.60      # v2: trailing stop activation frac
    trail_pct: float = 0.60           # v2: trailing stop trail frac
    max_hold_h: float = 18.0          # v2: time-stop hours
    # v2.1: decay counters for hysteresis exit
    composite_decay_hits: int = 0     # consecutive heartbeats where composite < threshold
    spi_decay_hits: int = 0           # consecutive heartbeats where spi decayed from entry
    # v2.2: regime captured at entry so coin-memory bucket is stable even
    # if the MIO regime shifts before exit.
    entry_regime: str = "UNKNOWN"
    # Phase 11n-9-ee: authz id bridges entry -> exit so the three-way
    # shadow scorer can mirror PnL into every variant that admitted.
    authz_id: str = ""


@dataclass
class EngineState:
    started_ts: int = 0
    cycles: int = 0
    mode: str = "live"
    capital_usd: float = WORKING_USD
    peak_equity: float = WORKING_USD
    current_equity: float = WORKING_USD
    dd_level: int = 0           # 0=normal, 1=caution, 2=defensive, 3=kill
    halted: bool = False
    halt_reason: str = ""
    blitz_active: bool = False
    blitz_count_24h: int = 0
    last_blitz_time: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    trades_today: int = 0
    frequency_ctrl: DailyFrequencyController = field(default_factory=DailyFrequencyController)
    # Funnel observability: in-memory ring of every scoring decision.
    # Each entry: (ts_ms, symbol, composite, tier_or_none)
    # 24h at ~1 cycle/min * 20 coins ≈ 28 800 rows → cap at 50 000 for safety.
    scoring_window: deque = field(default_factory=lambda: deque(maxlen=50000))
    # Phase D2β — last reconciliation summary (None until first reconcile).
    # Surfaces via engine.status() so the dashboard can show tracked vs
    # untracked truth. Never used to gate trading.
    reconciliation_summary: Optional[dict[str, Any]] = None


# Consensus cooldown is now per-tier (in TierConfig.cooldown_s)


class SpotAggroEngine:
    """SPOT AGGRO trading engine.

    Phase 11g — renamed from ``APEX_Spot_Aggro`` to ``SpotAggroEngine`` for
    clean spot-only ownership. ``APEX_Spot_Aggro`` remains available as a
    backward-compatibility alias (see bottom of this module) so any
    external importer (scripts, notebooks, unpushed branches) keeps
    working during rollout. New code must use ``SpotAggroEngine``.
    """

    def __init__(self, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self.state = EngineState(started_ts=int(time.time()))
        self._stop = asyncio.Event()
        self._adapter: Optional[OKXUnified] = None
        self._consensus_reject_ts: dict[str, float] = {}  # symbol → last rejection time
        self._stoploss_cooldown_ts: dict[str, float] = {}  # symbol → last SL exit time (300s cooldown)

    def _ensure_adapter(self) -> OKXUnified:
        if self._adapter is None:
            self._adapter = OKXUnified(engine="spot_aggro")
        return self._adapter

    def _snapshot_live_prices(self, symbols: list[str]) -> dict[str, float]:
        """Best-effort ticker snapshot for display-only live PnL. Never
        feeds exit decisions. On any error returns {}, the caller falls
        back to max_ret. Cached for 3s to keep status() cheap under the
        dashboard's 5s poll cadence."""
        if not symbols:
            return {}
        now = time.time()
        cache = getattr(self, "_live_price_cache", None)
        cache_ts = getattr(self, "_live_price_cache_ts", 0.0)
        if cache is not None and (now - cache_ts) < 3.0:
            return {s: cache[s] for s in symbols if s in cache}
        out: dict[str, float] = {}
        try:
            import asyncio
            adapter = self._ensure_adapter()
            async def _fetch_one(sym: str):
                try:
                    t = await adapter.get_spot_ticker(sym)
                    return sym, float(t.get("last") or 0)
                except Exception:  # noqa: BLE001
                    return sym, None
            async def _fetch_all():
                return await asyncio.gather(*[_fetch_one(s) for s in symbols])
            results = asyncio.run(_fetch_all())
            for sym, px in results:
                if px and px > 0:
                    out[sym] = px
        except Exception:  # noqa: BLE001
            return {}
        self._live_price_cache = out
        self._live_price_cache_ts = now
        return out

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        persist.init_schema()
        if not self.dry_run:
            try:
                adapter = self._ensure_adapter()
                eq = await adapter.get_account_equity()
                self.state.capital_usd = eq
                self.state.peak_equity = eq
                self.state.current_equity = eq
            except Exception as exc:
                log.warning("equity init failed: %s", exc)

            # Phase D2β — startup reconciliation. Pull real OKX holdings
            # + fills, reconstruct engine Position state so open_pairs /
            # tracked_count / holdings truth reconciles. No capital gate.
            # Failure is non-fatal (logged); engine continues on empty
            # positions, pre-flight guard will still prevent order spam.
            try:
                await self._reconcile_from_exchange()
            except Exception as exc:
                log.warning("reconciliation failed on boot: %s", exc)

        log.info("SPOT AGGRO start: capital=$%.2f mode=%s poll=%ds",
                 self.state.capital_usd, "paper" if self.dry_run else "live",
                 POLL_INTERVAL_S)
        notify.engine_start(mode="spot_aggro" + (" paper" if self.dry_run else " live"),
                            pairs_recovered=0, peak_usd=self.state.peak_equity)

        # Start 30-min MIO research cycle
        from .research import runner as research_runner
        research_runner.start(engine_ref=self)

        # Start 5-LLM swarm (3-layer: heavy/standard/fast)
        from .swarm import runner as swarm_runner
        swarm_runner.start(engine_ref=self)

        # Start forensic review (6-hourly PDF reports)
        from .forensic import runner as forensic_runner
        forensic_runner.start(engine_ref=self)

        while not self._stop.is_set():
            try:
                await self._heartbeat()
            except Exception:
                log.exception("heartbeat crashed (recovering)")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass

    async def halt(self, reason: str) -> None:
        self.state.halted = True
        self.state.halt_reason = reason
        notify.engine_halt(reason=reason)
        # Sell all positions
        for sym in list(self.state.positions.keys()):
            await self._close_position(sym, "halt")
        self._stop.set()

    # ------------------------------------------------------------------
    # Reconciliation (Phase D2β)
    # ------------------------------------------------------------------

    async def _reconcile_from_exchange(self) -> None:
        """Reconstruct Position state from real OKX fills on engine start.

        HIGH_CONFIDENCE holdings are loaded into self.state.positions with
        real entry price + entry time from fill history.

        LOW_CONFIDENCE holdings are also loaded (so open_pairs counters
        aren't a lie) but carry the reconstructed values with a caveat
        field — exits will still fire on TP/SL, operator is expected to
        review via the dashboard's reconciliation status card.

        RECONCILIATION_PENDING holdings are NOT loaded. The dashboard is
        responsible for surfacing that they exist on the exchange but
        aren't in engine state yet.

        Never raises. Never blocks trading. Never consults capital
        thresholds — this is execution-sufficiency infrastructure.
        """
        adapter = self._ensure_adapter()
        from .reconciliation import (
            reconcile_from_exchange,
            STATUS_HIGH_CONFIDENCE, STATUS_LOW_CONFIDENCE,
        )
        recs, summary = await reconcile_from_exchange(adapter)

        # Reconciled positions are loaded with SL/TP sentinels that CANNOT
        # fire. The exit loop treats these as "do not auto-exit — operator
        # review required" rows. Real SL/TP on reconstructed entries is
        # unsafe because (a) entry price comes from a weighted-average
        # reconstruction that can be off by 0.1–1.6% per OKX fee
        # accounting, and (b) we don't know the *intent* of those
        # holdings — some may be deliberate long-term holds from before
        # the engine knew about them.
        #
        # Sentinel convention: tp=+999.0 (never reachable), sl=-999.0
        # (never reachable). The exit logic in _heartbeat compares price
        # against these bounds; ±999 is outside any realistic price range.
        SENTINEL_TP = 999.0
        SENTINEL_SL = -0.999   # -99.9% — effectively unreachable
        loaded_hi = 0
        loaded_lo = 0
        pending = 0
        for symbol, r in recs.items():
            if r.status == STATUS_HIGH_CONFIDENCE:
                self.state.positions[symbol] = Position(
                    symbol=symbol, side="buy",
                    size_usd=r.size_usd,
                    entry_price=r.entry_price,
                    entry_spi=0.0,
                    entry_time=r.entry_time,
                    # Sentinel — reconciled positions never auto-exit on TP/SL
                    tp=SENTINEL_TP, sl=SENTINEL_SL,
                    is_blitz=False,
                    module="M_reconciled",
                    tier="?",
                    composite_score=0.0,
                    trail_activate=SENTINEL_TP, trail_pct=SENTINEL_TP,
                    max_hold_h=999999.0,      # effectively never time-stop
                    entry_regime="RECONCILED",
                )
                loaded_hi += 1
            elif r.status == STATUS_LOW_CONFIDENCE:
                self.state.positions[symbol] = Position(
                    symbol=symbol, side="buy",
                    size_usd=r.size_usd,
                    entry_price=r.entry_price,
                    entry_spi=0.0,
                    entry_time=r.entry_time,
                    tp=SENTINEL_TP, sl=SENTINEL_SL,
                    is_blitz=False,
                    module="M_reconciled_lowconf",
                    tier="?",
                    composite_score=0.0,
                    trail_activate=SENTINEL_TP, trail_pct=SENTINEL_TP,
                    max_hold_h=999999.0,
                    entry_regime="RECONCILED_LOWCONF",
                )
                loaded_lo += 1
            else:
                pending += 1

        self.state.reconciliation_summary = summary
        log.info(
            "[reconcile] loaded hi=%d lo=%d pending=%d (positions now %d)",
            loaded_hi, loaded_lo, pending, len(self.state.positions),
        )

    # ------------------------------------------------------------------
    # Heartbeat (spec §9)
    # ------------------------------------------------------------------

    async def _heartbeat(self) -> None:
        self.state.cycles += 1

        if app_settings.load().get("engine_paused"):
            return

        from .research.runner import get_mio
        mio = get_mio()
        # v2: MIO feeds into composite_score via scoring.py, NOT via apply_intelligence.
        # apply_intelligence was v1 — it overrode SPI gate based on regime (DEAD=0.80).
        # In v2, regime is a scoring input (weight 0.05), not a hard gate.

        # Forensic L3 timeline — sample regime when it changes OR every 60s.
        # Writing every heartbeat (2s) would flood the DB; sampling on change
        # captures transitions without noise.
        try:
            now = time.time()
            regime_label = getattr(mio, "regime", None) or "UNKNOWN"
            last_regime = getattr(self, "_last_regime_sample_label", None)
            last_ts = getattr(self, "_last_regime_sample_ts", 0.0)
            if regime_label != last_regime or (now - last_ts) >= 60:
                persist.log_regime_sample(
                    regime=regime_label,
                    regime_confidence=getattr(mio, "regime_confidence", None),
                    # MarketIntelligence uses `squeeze_timing_window`, not
                    # `squeeze_timing`; fall through both names safely.
                    squeeze_timing=(
                        getattr(mio, "squeeze_timing_window", None)
                        or getattr(mio, "squeeze_timing", None)
                    ),
                    edge_status=getattr(mio, "edge_status", None),
                    universe_quality=getattr(mio, "universe_quality", None),
                )
                try:
                    from .telemetry import emit_regime as _emit_regime
                    _emit_regime(
                        regime=regime_label,
                        regime_confidence=getattr(mio, "regime_confidence", None),
                        squeeze_timing=(
                            getattr(mio, "squeeze_timing_window", None)
                            or getattr(mio, "squeeze_timing", None)
                        ),
                        edge_status=getattr(mio, "edge_status", None),
                        universe_quality=getattr(mio, "universe_quality", None),
                    )
                except Exception:
                    pass
                self._last_regime_sample_label = regime_label
                self._last_regime_sample_ts = now
        except Exception:
            log.debug("regime sample failed (non-fatal)", exc_info=True)

        await self._update_equity()
        self._update_dd_level()
        if self.state.dd_level >= 3:
            await self.halt(f"kill: DD > {KILL_DD_PCT*100:.0f}%")
            return

        # Phase 11n-9-hh follow-up: in dry_run (paper) mode we used to
        # short-circuit here and do nothing. That meant paper mode was
        # "idle" — no ranking, no authz, no shadow rows. That made the
        # three-way horse race impossible to fuel.
        #
        # Now we fall through to the full heartbeat. The actual order-
        # placement sites (adapter.place_post_only, exit fills) already
        # check self.dry_run and skip the exchange call. So in paper
        # mode we get:
        #   - real rankings from live market data
        #   - real composite scores, tiers, pre-trade authz
        #   - real three-way shadow rows for every authz
        #   - simulated fills (no real capital touched)

        adapter = self._ensure_adapter()

        # Rank universe by SPI (cached 60s)
        rankings = await self._rank_by_spi(adapter)

        # v2: Compute composite scores + classify tiers
        tiered = self._classify_universe(rankings, mio)

        # Check exits on all open positions (v2.1: composite-aware exits)
        await self._check_exits_v2(adapter, rankings, mio)

        # Frequency controller day-reset
        self.state.frequency_ctrl.reset_if_new_day()

        # Scan for new entries (v2: tiered — A+ first, then A, B, C)
        if len(self.state.positions) < MAX_POSITIONS:
            await self._scan_entries_tiered(adapter, tiered, mio)

    # ------------------------------------------------------------------
    # SPI ranking
    # ------------------------------------------------------------------

    _rank_cache: list[dict[str, Any]] = []
    _rank_cache_ts: float = 0.0

    async def _rank_by_spi(self, adapter: OKXUnified) -> list[dict[str, Any]]:
        # Cache for 60s to avoid 80 REST calls per heartbeat
        if self._rank_cache and (time.time() - self._rank_cache_ts) < 60:
            return list(self._rank_cache)
        cfg = load_cfg()
        symbols = [u["symbol"] for u in cfg.get("universe", [])]
        if not symbols:
            from shared.adapters import universe_discovery
            cached_universe = universe_discovery.cached_universe
            symbols = [c.symbol for c in cached_universe()[:30]]

        ranked = []
        for sym in symbols[:20]:    # top 20 by prelim
            try:
                # Funding from PERP (public data), price+depth from SPOT
                fr = await adapter.get_funding(sym)
                tick = await adapter.get_spot_ticker(sym)
                funding_rate = float(fr.get("rate") or 0)
                price = float(tick["last"])
                hist = await adapter.get_funding_history(sym, limit=90)
                mu = sum(hist) / len(hist) if hist else 0
                sig = (sum((x - mu) ** 2 for x in hist) / max(len(hist), 1)) ** 0.5 if hist else 1e-4
                fz = funding_rate / max(sig, 1e-9)

                # Fetch real data for SPI components (replaces placeholders)
                oi_change = await adapter.get_open_interest_change(sym)
                ret_7d = await adapter.get_price_return_7d(sym)
                # Phase 11n-9-pp — momentum variant needs short-horizon
                # returns. Single OHLCV call gives both 24h and 4h.
                try:
                    ret_24h, ret_4h = await adapter.get_price_return_24h_4h(sym)
                except Exception:
                    ret_24h, ret_4h = 0.0, 0.0
                low_24h = await adapter.get_recent_low(sym)
                liq_est = low_24h if low_24h > 0 else price * 0.97

                spi_val, components = spi_mod.compute_spi(
                    funding_z=fz,
                    oi_growth_rate=oi_change,
                    price_ret_7d=ret_7d,
                    funding_avg_7d=mu,
                    price=price,
                    liq_cluster_price=liq_est,
                )
                depth = await adapter.get_spot_book_depth_usd(sym, levels=5)

                ranked.append({
                    "symbol": sym, "spi": spi_val, "components": components,
                    "funding_z": fz, "funding_rate": funding_rate,
                    "price": price, "depth_usd": depth,
                    "spread_bp": float(tick.get("spread_bp") or 0),
                    "mu_30d": mu, "sigma_30d": sig,
                    "oi_change": oi_change, "ret_7d": ret_7d,
                    # Phase 11n-9-pp — short-horizon returns for momentum.
                    "return_24h": ret_24h,
                    "return_4h": ret_4h,
                })
            except Exception as exc:
                log.warning("rank %s failed: %s", sym, exc)
                continue

        ranked.sort(key=lambda r: -r["spi"])
        self._rank_cache = ranked
        self._rank_cache_ts = time.time()
        log.info("ranked %d/%d coins (top: %s)", len(ranked), len(symbols[:20]),
                 ranked[0]["symbol"] if ranked else "none")
        return ranked

    # ------------------------------------------------------------------
    # v2: Composite scoring + tier classification
    # ------------------------------------------------------------------

    def _classify_universe(self, rankings: list[dict], mio) -> list[tuple[dict, TierConfig]]:
        """Score all coins, classify into tiers, return sorted by composite desc."""
        from .swarm.integration import swarm_composite_adjustment, swarm_tier_override
        thresholds = self.state.frequency_ctrl.adaptive_thresholds()
        tiered = []
        now_ms = int(time.time() * 1000)
        regime_label = getattr(mio, "regime", "UNKNOWN") or "UNKNOWN"
        for r in rankings:
            composite = compute_composite_score(r, mio)
            # Swarm blends buy_confidence into composite (30% weight when fresh)
            composite = swarm_composite_adjustment(r["symbol"], composite)
            tc = classify_tier(composite, r["spi"], thresholds)
            # Layer 1 — soft win-rate multiplier (±10% max, confidence-weighted).
            # Applied using the tier the coin is *currently* classified into and
            # the *current* regime. Neutral 1.0 when bucket is new/sparse.
            if tc is not None:
                mult = coin_memory.composite_multiplier(
                    r.get("symbol", ""), tc.tier, regime_label,
                )
                if mult != 1.0:
                    composite *= mult
                    # Re-classify in case the nudge crossed a tier boundary.
                    tc = classify_tier(composite, r["spi"], thresholds)
            # Funnel telemetry — record every scoring decision before tier filter.
            self.state.scoring_window.append(
                (now_ms, r.get("symbol", ""), float(composite),
                 tc.tier if tc is not None else None)
            )
            if tc is not None:
                # Layer 2 + 3 — cooldown / suppression check.
                skip, reason = coin_memory.should_skip(
                    r.get("symbol", ""), tc.tier, regime_label,
                )
                if skip:
                    log.info("coin_memory skip %s@%s@%s: %s",
                             r.get("symbol"), tc.tier, regime_label, reason)
                    continue
                # Swarm can promote/demote tier (STRONG_BUY→promote, AVOID→skip)
                new_tier = swarm_tier_override(r["symbol"], tc.tier)
                if new_tier == "SKIP":
                    continue
                if new_tier != tc.tier and new_tier in TIER_PARAMS:
                    tc = TIER_PARAMS[new_tier]
                r["composite"] = composite
                r["tier"] = tc.tier
                r["_entry_regime"] = regime_label  # stash for Position entry
                tiered.append((r, tc))
        tiered.sort(key=lambda x: -x[0]["composite"])
        self._tiered_cache = tiered
        return tiered

    # ------------------------------------------------------------------
    # v2: Tiered entry scan — A+ first, then A, B, C
    # ------------------------------------------------------------------

    async def _scan_entries_tiered(self, adapter: OKXUnified,
                                   tiered: list[tuple[dict, TierConfig]],
                                   mio) -> None:
        deployed = sum(p.size_usd for p in self.state.positions.values())
        for r, tc in tiered:
            if len(self.state.positions) >= MAX_POSITIONS:
                break
            if deployed >= self.state.capital_usd * DEPLOY_CEIL:
                break
            if r["symbol"] in self.state.positions:
                continue

            # Candidate viability (v2: minimal hard gates)
            if r["depth_usd"] < 500:
                continue

            # Stop-loss cooldown — don't re-enter a coin within 5 min of SL exit
            sl_ts = self._stoploss_cooldown_ts.get(r["symbol"], 0)
            if time.time() - sl_ts < 300:
                continue

            # Swarm entry gate — blocks AVOID / low-confidence SKIP
            from .swarm.integration import swarm_entry_gate, swarm_sizing_multiplier
            gate_ok, gate_reason = swarm_entry_gate(r["symbol"])
            if not gate_ok:
                log.debug("swarm gate blocked %s: %s", r["symbol"], gate_reason)
                continue
            if r["price"] <= 0:
                continue

            # Frequency controller
            if not self.state.frequency_ctrl.can_enter(tc.tier):
                continue

            # Per-tier consensus cooldown
            last_reject = self._consensus_reject_ts.get(r["symbol"], 0)
            if time.time() - last_reject < tc.cooldown_s:
                continue

            # A+ (BLITZ) additional guards
            if tc.tier == "A+":
                if self.state.blitz_active:
                    continue
                if self.state.dd_level >= 1:
                    continue
                if time.time() - self.state.last_blitz_time < BLITZ_COOLDOWN_S:
                    continue

            # Phase 11n-9-bb: swarm prefilter — skip the expensive
            # 5-LLM consensus unless this coin × tier can actually
            # trade right now. Cuts API spend ~95% when the admitted
            # universe is small.
            from .swarm.prefilter import should_call_llm
            allow_llm, prefilter_reason = should_call_llm(
                r["symbol"], layer=f"consensus:{tc.tier}",
            )
            if not allow_llm:
                # Also honor per-tier cell admission specifically (not
                # just "any tier admitted for this symbol"): the coin
                # may be admitted on Tier C but the current candidate
                # is for Tier B, etc.
                log.debug("swarm consensus skipped %s tier=%s: %s",
                          r["symbol"], tc.tier, prefilter_reason)
                continue
            try:
                from spot_aggro.governance.universe_gatekeeper import is_cell_admitted
                cell_key = f"{tc.tier}|{r['symbol']}"
                if not is_cell_admitted("tier_symbol", cell_key):
                    log.debug("swarm consensus skipped %s: cell %s not admitted",
                              r["symbol"], cell_key)
                    continue
            except Exception:
                # Fail-closed on gatekeeper probe error — don't pay
                # for LLMs when we can't verify admission.
                continue

            # Per-tier LLM consensus (member_count varies by tier)
            ctx = {**r, "ret_24h": 0, "ret_7d": 0, "oi_usd": 0, "oi_change": 0,
                   "spi_fz": r["components"]["fz"], "spi_oi": r["components"]["oi"],
                   "spi_div": r["components"]["div"], "spi_liq": r["components"]["liq"],
                   "liq_1h": 0, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "funding_sigma": r["sigma_30d"] * 100}
            c = await llm_consensus.run_consensus(
                symbol=r["symbol"], ctx=ctx,
                member_count=tc.consensus_depth,
            )
            notify.consensus_fired(symbol=r["symbol"], consensus=c.consensus,
                                   conflict=c.conflict, vetoed=c.vetoed,
                                   members_called=c.members_called)

            # Per-tier consensus gates
            if tc.veto_enabled and c.vetoed:
                self._consensus_reject_ts[r["symbol"]] = time.time()
                continue
            if c.consensus < tc.consensus_min:
                self._consensus_reject_ts[r["symbol"]] = time.time()
                continue
            if c.conflict > tc.conflict_max:
                self._consensus_reject_ts[r["symbol"]] = time.time()
                continue

            # Per-tier sizing
            size = self._compute_size_tiered(c.consensus, c.conflict, r["spi"], tc)
            size *= swarm_sizing_multiplier(r["symbol"])  # swarm can scale 0.5x-1.5x
            if size < 5:
                continue

            # Per-tier TP/SL
            tp, sl = spi_mod.compute_tp_sl_tiered(r["spi"], tc.tp_mult, tc.sl_mult)

            # Phase D2α — pre-flight execution-sufficiency guard.
            #
            # The OLD guard computed available = total_equity - tracked_size.
            # That lies when the engine's tracked positions are incomplete
            # (e.g. after a state reset where real holdings exist on OKX
            # but the engine's state.positions dict is empty). It would pass
            # the local check and then collide with OKX 51008 Insufficient
            # USDT — producing the 282/539 reject storm.
            #
            # The fix is to ask OKX for FREE USDT directly. That is the
            # only value that gates order placement at the exchange.
            #
            # If free USDT < required notional we skip with a telemetry
            # event. The skip is NOT an exchange reject — we never send
            # the order to OKX, so no 51008 comes back. This is execution
            # sufficiency, not a capital lock: there is no minimum-equity
            # threshold, no startup block, no blanket refusal. A trade
            # that we could actually fill is still submitted normally.
            try:
                free_usdt = await adapter.get_free_usdt()
            except Exception as exc:
                free_usdt = None
                log.warning("pre-flight: free-USDT probe failed — %s", exc)

            if free_usdt is not None and size > free_usdt:
                reason_code = (
                    "INSUFFICIENT_FREE_USDT" if free_usdt < 1.0
                    else "SKIPPED_NO_BUYING_POWER"
                )
                log.info(
                    "pre-flight SKIP %s: size $%.2f > free_usdt $%.4f (%s)",
                    r["symbol"], size, free_usdt, reason_code,
                )
                # Telemetry-only skip row. Distinct from 'reject' (which
                # means OKX said no). Dashboards can count skip != reject.
                persist.log_trade(
                    symbol=r["symbol"], module=tc.module, action="skip",
                    tier=tc.tier,
                    payload={
                        "reason": reason_code,
                        "free_usdt": round(free_usdt, 4),
                        "required_size": round(size, 4),
                        "tier": tc.tier,
                        "composite": r.get("composite"),
                        "note": "not sent to OKX — execution sufficiency guard",
                    },
                )
                try:
                    from .telemetry import emit_trade as _emit_trade
                    _emit_trade(
                        "skip", symbol=r["symbol"], tier=tc.tier,
                        module=tc.module, notional_usd=size,
                        reason=reason_code, composite=r.get("composite"),
                        spi=r.get("spi"),
                    )
                except Exception:
                    pass
                continue

            # Phase 11n-9-hh follow-up: record three-way shadow BEFORE
            # the readiness/universe/freeze gates. The horse race needs
            # a decision row for every coin that made it through
            # consensus, regardless of whether downstream gates let it
            # trade. Uses a synthetic authz_id since the real authz is
            # downstream; the engine-path authz record still comes when
            # authorize_trade fires below.
            try:
                import uuid as _uuid
                _shadow_authz_id = f"shadow-{int(time.time()*1000)}-{_uuid.uuid4().hex[:6]}"
                from spot_aggro.governance.three_way_shadow import (
                    record_authz as _tw_record_authz_early,
                )
                from .research.runner import get_mio as _get_mio_early
                _tw_record_authz_early(
                    live_authz_id=_shadow_authz_id,
                    symbol=r["symbol"], side="buy", tier=str(tc.tier),
                    coin=r, mio=_get_mio_early(),
                )
            except Exception as _tw_early_err:  # noqa: BLE001
                log.debug(
                    "three_way_shadow early record failed: %s",
                    _tw_early_err,
                )

            # Phase 11n-9-aa: Trade Readiness gate — the mechanical
            # release flag. Must pass C1..C5 before anything else
            # runs. is_ready_to_trade() is a derived read; the
            # readiness daemon keeps it fresh. Fail-closed on any
            # probe error.
            #
            # Phase 11n-9-hh follow-up: SPOT_SHADOW_COLLECT=1 bypasses
            # this gate for paper-mode shadow collection. In dry_run
            # the bypass is safe because synthesized receipts never
            # hit the exchange. Live mode ignores the flag.
            import os as _os
            _shadow_collect = (
                _os.environ.get("SPOT_SHADOW_COLLECT", "0").strip() == "1"
                and self.dry_run
            )
            try:
                from spot_aggro.governance.trade_readiness import is_ready_to_trade
                if not is_ready_to_trade() and not _shadow_collect:
                    try:
                        _emit_trade(
                            "skip", symbol=r["symbol"], tier=tc.tier,
                            module=tc.module, notional_usd=size,
                            reason="not_ready_to_trade",
                            composite=r.get("composite"), spi=r.get("spi"),
                        )
                    except Exception:
                        pass
                    continue
            except Exception:  # noqa: BLE001
                if not _shadow_collect:
                    try:
                        _emit_trade(
                            "skip", symbol=r["symbol"], tier=tc.tier,
                            module=tc.module, notional_usd=size,
                            reason="trade_readiness_probe_error",
                            composite=r.get("composite"), spi=r.get("spi"),
                        )
                    except Exception:
                        pass
                    continue

            # Phase 11n-9-aa: Universe Gatekeeper — cell must be in
            # admitted state. Seed cells are Tier-C + {ENA, DOT}; the
            # universe expands by ratchet rule as cells prove edge.
            # Phase 11n-9-hh follow-up: SPOT_SHADOW_COLLECT bypass.
            try:
                from spot_aggro.governance.universe_gatekeeper import is_cell_admitted
                cell_key = f"{tc.tier}|{r['symbol']}"
                if not is_cell_admitted("tier_symbol", cell_key) and not _shadow_collect:
                    try:
                        _emit_trade(
                            "skip", symbol=r["symbol"], tier=tc.tier,
                            module=tc.module, notional_usd=size,
                            reason=f"cell_not_admitted:{cell_key}",
                            composite=r.get("composite"), spi=r.get("spi"),
                        )
                    except Exception:
                        pass
                    continue
            except Exception:  # noqa: BLE001
                # Fail-closed on gatekeeper probe error.
                if not _shadow_collect:
                    continue

            # Phase 11n-9-y: Layer 3 Contradiction Freeze gate — must
            # run BEFORE the Layer 8 pre-trade check so a freeze shuts
            # down the entry path even when the pre-trade gate would
            # have admitted. Fail-closed (is_entry_frozen returns True
            # on any DB read failure).
            try:
                from spot_aggro.governance.contradiction_freeze import is_entry_frozen
                if is_entry_frozen():
                    try:
                        _emit_trade(
                            "skip", symbol=r["symbol"], tier=tc.tier,
                            module=tc.module, notional_usd=size,
                            reason="contradiction_freeze_active",
                            composite=r.get("composite"), spi=r.get("spi"),
                        )
                    except Exception:
                        pass
                    continue
            except Exception:  # noqa: BLE001
                # Any fault reading the freeze state is treated as
                # frozen — never default to trading through.
                try:
                    _emit_trade(
                        "skip", symbol=r["symbol"], tier=tc.tier,
                        module=tc.module, notional_usd=size,
                        reason="contradiction_freeze_probe_error",
                        composite=r.get("composite"), spi=r.get("spi"),
                    )
                except Exception:
                    pass
                continue

            # Phase 11n-9-b: Pre-Trade Governor (Layer 8) gates EVERY
            # buy. Fails the trade with an explicit reject reason if
            # any checklist item blocks. Every decision is persisted so
            # the dashboard shows what the gov allowed vs blocked.
            # Phase 11n-9-y: bypass now raises GateBlocked — an explicit
            # named exception so any code path that forgets to call the
            # gate is visibly wrong during regression testing.
            # Phase 11n-9-ii — Live variant gate. When
            # SPOT_LIVE_VARIANTS=contrarian,deep_value is set, the
            # engine switches from the control composite scorer to
            # OR-logic across the named variants + enforces $50 total
            # exposure cap + $10 live-session-DD kill. In paper mode
            # the gate is advisory only.
            _lv_admitted = False
            _lv_admitting_variant: str | None = None
            try:
                from spot_aggro.governance import live_variant_gate as _lvg
                if _lvg.live_variants_active() and not self.dry_run:
                    from .research.runner import get_mio as _lvg_get_mio
                    _lv_verdict = _lvg.evaluate(
                        coin=r, mio=_lvg_get_mio(),
                        candidate_size_usd=size,
                    )
                    if not _lv_verdict.ok:
                        try:
                            _emit_trade(
                                "skip", symbol=r["symbol"], tier=tc.tier,
                                module=tc.module, notional_usd=size,
                                reason=f"live_variant_block:{_lv_verdict.reason}"[:120],
                                composite=r.get("composite"), spi=r.get("spi"),
                            )
                        except Exception:
                            pass
                        continue
                    _lv_admitted = True
                    _lv_admitting_variant = _lv_verdict.admitting_variant
                    # Phase 11n-9-oo — apply meta-gate size multiplier.
                    size_mult = float(
                        getattr(_lv_verdict, "size_multiplier", 1.0) or 1.0
                    )
                    if size_mult != 1.0:
                        old_size = size
                        size = round(size * size_mult, 2)
                        log.info(
                            "meta size adjust %s: $%.2f -> $%.2f (mult=%.2f regime=%s)",
                            r["symbol"], old_size, size, size_mult,
                            getattr(_lv_verdict, "regime", "?"),
                        )
                    # Skip tiny orders below $2 notional (OKX min ~$1; fees dominate).
                    if size < 2.0:
                        log.info(
                            "meta size too small %s: $%.2f < $2 floor (skip)",
                            r["symbol"], size,
                        )
                        continue
                    log.info(
                        "live variant admit: %s (%s) size=$%.2f regime=%s",
                        r["symbol"], _lv_admitting_variant, size,
                        getattr(_lv_verdict, "regime", "?"),
                    )
                    try:
                        _lvg.record_variant_entry(
                            variant=_lv_admitting_variant,
                            symbol=r["symbol"],
                            notional_usd=size,
                        )
                    except Exception:
                        pass
            except Exception as _lvg_err:  # noqa: BLE001
                log.warning(
                    "live_variant_gate error (fail-closed skip): %s",
                    _lvg_err,
                )
                if not self.dry_run:
                    continue

            _live_authz_id = ""
            try:
                from spot_aggro.governance import pre_trade_gov
                authz = pre_trade_gov.authorize_trade(
                    r["symbol"], "buy", tc.tier,
                    source=f"engine_entry:{tc.module}",
                )
                _live_authz_id = str(getattr(authz, "authz_id", ""))
                # Phase 11n-9-ee — three-way shadow horse race. Every
                # authz records what control / contrarian / mean_reversion
                # would have done. Never blocks the live path (fail-open).
                try:
                    from spot_aggro.governance.three_way_shadow import (
                        record_authz as _tw_record_authz,
                    )
                    # mio is a local variable in _heartbeat; the scan
                    # path doesn't carry it directly — pull a fresh one.
                    from .research.runner import get_mio as _get_mio
                    _tw_record_authz(
                        live_authz_id=_live_authz_id,
                        symbol=r["symbol"], side="buy", tier=str(tc.tier),
                        coin=r, mio=_get_mio(),
                    )
                except Exception as _tw_err:  # noqa: BLE001
                    log.debug(
                        "three_way_shadow record_authz failed (non-fatal): %s",
                        _tw_err,
                    )
                if not authz.passed:
                    # Phase 11n-9-ii option-G: when live_variant_gate already
                    # admitted (contrarian/deep_value), bypass Layer 8 pre-
                    # trade reject. Contrarian's thesis directly contradicts
                    # Layer 8's projected-WR gate — a coin with 38% WR is
                    # EXACTLY what contrarian wants. Live_variant_gate has
                    # already enforced $50 cap + $10 DD kill + kill-ladder.
                    # Layer 8 still records the authz for audit trail; we
                    # just don't act on its reject verdict.
                    if _lv_admitted:
                        try:
                            _emit_trade(
                                "bypass_L8", symbol=r["symbol"], tier=tc.tier,
                                module=tc.module, notional_usd=size,
                                reason=(
                                    f"live_variant_gate_admit:{_lv_admitting_variant}"
                                    f"|L8_override:{(authz.rejection_reason or 'unknown')[:40]}"
                                )[:120],
                                composite=r.get("composite"), spi=r.get("spi"),
                            )
                        except Exception:
                            pass
                        log.warning(
                            "option-G L8 BYPASS: %s (variant=%s, L8_reason=%s) size=$%.2f",
                            r["symbol"], _lv_admitting_variant,
                            (authz.rejection_reason or "?")[:40], size,
                        )
                    else:
                        try:
                            _emit_trade(
                                "skip", symbol=r["symbol"], tier=tc.tier,
                                module=tc.module, notional_usd=size,
                                reason=f"pre_trade_gov_block:{authz.rejection_reason}",
                                composite=r.get("composite"), spi=r.get("spi"),
                            )
                        except Exception:
                            pass
                        continue
            except Exception:  # noqa: BLE001
                # If the governor itself errors, fail CLOSED: skip the
                # trade. Never default to letting trades through on a
                # governor fault.
                try:
                    _emit_trade(
                        "skip", symbol=r["symbol"], tier=tc.tier,
                        module=tc.module, notional_usd=size,
                        reason="pre_trade_gov_error",
                        composite=r.get("composite"), spi=r.get("spi"),
                    )
                except Exception:
                    pass
                continue

            # Phase 11n-9-ff — Layer 1 Execution Integrity. Three hard
            # gates: kill-ladder L0 check, 2% per-trade risk cap, and
            # ±15bp price tolerance. Any failure records a reject event
            # so the auto-pause storm detector can escalate.
            try:
                from spot_aggro.governance import execution_integrity as _ei
                _ei_verdict = _ei.check_all(
                    notional_usd=size,
                    equity_usd=self.state.capital_usd,
                    requested_px=r["price"],
                    reference_px=r["price"],
                    symbol=r["symbol"],
                )
            except Exception as _ei_err:  # noqa: BLE001
                _ei_verdict = None
                log.warning(
                    "execution_integrity check raised (fail-closed skip): %s",
                    _ei_err,
                )
            if _ei_verdict is None or not _ei_verdict.ok:
                reason = (
                    _ei_verdict.reason if _ei_verdict is not None
                    else "execution_integrity_error"
                )
                try:
                    _emit_trade(
                        "skip", symbol=r["symbol"], tier=tc.tier,
                        module=tc.module, notional_usd=size,
                        reason=f"exec_integrity_block:{reason}"[:120],
                        composite=r.get("composite"), spi=r.get("spi"),
                    )
                except Exception:
                    pass
                continue

            # Place spot buy. Phase 11n-9-hh follow-up: in dry_run (paper)
            # mode we synthesize an OK fill receipt here instead of
            # calling the real adapter.
            #
            # Phase 11n-9-jj — when live_variant_gate has admitted, we
            # route through place_market_verified instead of
            # place_post_only. Post-only returned ok=True on orders
            # that sat in the book unfilled, creating phantom positions.
            # The verified path polls for actual fill before returning
            # ok=True, so position state tracks OKX reality.
            if self.dry_run:
                from shared.adapters.okx_unified import OrderReceipt as _OR
                receipt = _OR(
                    ok=True,
                    order_id=f"paper-{int(time.time()*1000)}-{r['symbol']}",
                    cl_ord_id=None,
                    filled_qty=size / max(r["price"], 1e-9),
                    avg_px=r["price"],
                    side="buy",
                    symbol=r["symbol"],
                    notional_usd=size,
                    fee_usd=0.0,
                    raw={"paper": True},
                    error=None,
                )
            elif _lv_admitted:
                # Live + variant-gate admit → verified market order.
                receipt = await adapter.place_market_verified(
                    symbol=r["symbol"], side="buy", notional_usd=size,
                    reference_price=r["price"], leg="spot",
                    idempotency_seed=f"sajj:{r['symbol']}:{int(time.time()//60)}",
                )
            else:
                receipt = await adapter.place_post_only(
                    symbol=r["symbol"], side="buy", notional_usd=size,
                    reference_price=r["price"], leg="spot",
                    idempotency_seed=f"sa:{r['symbol']}:{int(time.time()//60)}",
                )
            if not receipt.ok:
                # Phase 11b final — reject rows carry tier explicitly so
                # the heatmap's canonical-activity lane can count
                # per-tier reject rates (previously rejects landed in
                # OFF-ENUM because `payload` had no "tier" field).
                persist.log_trade(
                    symbol=r["symbol"], module=tc.module, action="reject",
                    tier=tc.tier,
                    payload={
                        "error": receipt.error,
                        "tier": tc.tier,
                        "composite": r.get("composite"),
                    },
                )
                try:
                    from .telemetry import emit_trade as _emit_trade
                    _emit_trade(
                        "reject", symbol=r["symbol"], tier=tc.tier,
                        module=tc.module, notional_usd=size,
                        reason=(receipt.error or "unknown")[:64],
                        composite=r.get("composite"), spi=r.get("spi"),
                    )
                except Exception:
                    pass
                notify.pair_reject(symbol=r["symbol"], module=tc.module,
                                   error=receipt.error or "unknown", leg="spot")
                # Phase 11n-9-ff — count exchange rejects toward the
                # auto-pause storm detector.
                try:
                    from spot_aggro.governance.kill_ladder import record_reject
                    record_reject(
                        kind="reject",
                        symbol=r["symbol"],
                        detail={
                            "source": "exchange",
                            "error": (receipt.error or "unknown")[:120],
                            "module": tc.module,
                            "tier": tc.tier,
                        },
                    )
                except Exception:
                    pass
                continue

            # Open position with tier metadata
            pos = Position(
                symbol=r["symbol"], side="buy", size_usd=size,
                entry_price=r["price"], entry_spi=r["spi"],
                entry_time=time.time(), tp=tp, sl=sl,
                is_blitz=(tc.tier == "A+"),
                module=tc.module,
                tier=tc.tier,
                composite_score=r["composite"],
                trail_activate=tc.trail_activate,
                trail_pct=tc.trail_pct,
                max_hold_h=tc.max_hold_h,
                entry_regime=r.get("_entry_regime", "UNKNOWN"),
                authz_id=_live_authz_id,
            )
            # Phase 11n-9-hh — hash-chained immutable ledger. One entry
            # row per successful open. Never blocks live path.
            try:
                from spot_aggro.governance.immutable_ledger import append as _il_append
                _il_append(
                    "entry", symbol=r["symbol"], tier=tc.tier,
                    notional_usd=size,
                    correlation_id=_live_authz_id,
                    payload={
                        "module": tc.module,
                        "composite": r.get("composite"),
                        "spi": r.get("spi"),
                        "entry_price": r["price"],
                    },
                )
            except Exception:
                pass
            self.state.positions[r["symbol"]] = pos
            deployed += size
            self.state.trades_today += 1
            self.state.frequency_ctrl.record_entry(tc.tier)

            # BLITZ bookkeeping for A+
            if tc.tier == "A+":
                self.state.blitz_active = True
                self.state.blitz_count_24h += 1
                self.state.last_blitz_time = time.time()
                log.warning("BLITZ FIRED: %s $%.2f spi=%.3f composite=%.3f",
                            r["symbol"], size, r["spi"], r["composite"])

            log.info("ENTRY [%s] %s $%.2f spi=%.3f composite=%.3f consensus=%.3f",
                     tc.tier, r["symbol"], size, r["spi"], r["composite"], c.consensus)
            # Forensic-truth snapshot — every field the L1-L4 specialists
            # need MUST be captured at entry. Snapshotting after the fact
            # is unreliable because regime/swarm state evolves.
            try:
                from .swarm.runner import get_coin_intel
                ci = get_coin_intel(r["symbol"])
            except Exception:
                ci = None
            persist.log_trade(symbol=r["symbol"], module=tc.module, action="enter",
                              side="buy", notional_usd=size, avg_px=r["price"],
                              tier=tc.tier,
                              payload={
                                  "spi": r["spi"],
                                  "consensus": c.consensus,
                                  "tier": tc.tier,
                                  "composite": r["composite"],
                                  "tp": tp, "sl": sl,
                                  # Forensic snapshot fields:
                                  "entry_regime": r.get("_entry_regime") or
                                      getattr(mio, "regime", "UNKNOWN") or "UNKNOWN",
                                  "regime_confidence": getattr(mio, "regime_confidence", None),
                                  "composite_score": r["composite"],
                                  "swarm_action_at_entry":
                                      getattr(ci, "final_action", None) if ci else None,
                                  "swarm_confidence_at_entry":
                                      getattr(ci, "buy_confidence", None) if ci else None,
                                  "swarm_agents_ok_at_entry":
                                      getattr(ci, "agents_ok", None) if ci else None,
                                  "swarm_agents_total_at_entry":
                                      getattr(ci, "agents_total", None) if ci else None,
                                  "swarm_layer_at_entry":
                                      getattr(ci, "layer", None) if ci else None,
                              })
            try:
                from .telemetry import emit_trade as _emit_trade
                _emit_trade(
                    "enter", symbol=r["symbol"], tier=tc.tier,
                    module=tc.module, notional_usd=size, avg_px=r["price"],
                    composite=r.get("composite"), spi=r.get("spi"),
                )
            except Exception:
                pass
            notify.pair_enter(symbol=r["symbol"], module=tc.module,
                              notional_usd=size, side="buy",
                              consensus=c.consensus, abs_z=abs(r["funding_z"]))

    # ------------------------------------------------------------------
    # Sizing (spec §2.3)
    # ------------------------------------------------------------------

    def _compute_size(self, consensus: float, conflict: float, spi: float) -> float:
        """v1 sizing (kept for reference)."""
        base = self.state.capital_usd * DEPLOY_CEIL / MAX_POSITIONS
        ai = 1.0 / (1.0 + math.exp(-10 * (consensus - 0.50)))
        sq = max(0.40, min((spi - 0.40) / 0.40, 1.0))
        dd_24h = (self.state.peak_equity - self.state.current_equity) / max(self.state.peak_equity, 1)
        risk = max(0.10, (1 - dd_24h * 1.5) * (1 - conflict))
        return base * ai * sq * risk

    def _compute_size_tiered(self, consensus: float, conflict: float,
                             spi: float, tc: TierConfig) -> float:
        """v2 tier-aware sizing. Max fraction of capital varies by tier."""
        base = self.state.capital_usd * tc.max_size_frac
        ai = 1.0 / (1.0 + math.exp(-8 * (consensus - 0.30)))
        sq = max(0.40, min((spi - 0.20) / 0.60, 1.0))
        dd_24h = (self.state.peak_equity - self.state.current_equity) / max(self.state.peak_equity, 1)
        risk = max(0.10, (1.0 - dd_24h * 2.0) * (1.0 - conflict * 0.5))
        scale = getattr(self, '_position_scale', 1.0)
        size = base * ai * sq * risk * scale
        return max(5.0, min(size, base))

    # ------------------------------------------------------------------
    # v2: Per-tier exits
    # ------------------------------------------------------------------

    async def _check_exits_v2(self, adapter: OKXUnified, rankings: list[dict], mio=None) -> None:
        """v2.1: Composite-aware exits with hysteresis.

        Exit hierarchy (checked in order):
          1. TP hit           — immediate
          2. SL hit           — immediate
          3. Trailing stop    — immediate (per-tier thresholds)
          4. Time-stop        — immediate (per-tier max_hold_h)
          5. COMPOSITE_DECAY  — 2 consecutive heartbeats where
                                composite_now < max(0.18, entry_composite * 0.55)
          6. SPI_DECAY        — 3 consecutive heartbeats where
                                spi_now < max(0.08, entry_spi * 0.55)
                                AND composite also below entry * 0.70
        """
        rmap = {r["symbol"]: r for r in rankings}
        to_exit = []

        for sym, pos in self.state.positions.items():
            # Phase 11d final — reconciled positions never auto-exit under
            # ANY path: not TP, not SL, not TRAIL, not TIME_STOP, not
            # COMPOSITE_DECAY, not SPI_DECAY. They were reconstructed from
            # real exchange fills with no entry-time plan, so engine-driven
            # exits would trade them against intent. The previous placement
            # below the hard-exit block allowed a non-sentinel SL (e.g. a
            # default -0.012 from a regression) to close them and book a
            # real cash loss — which is how the 11 historical reconciled
            # SL exits totalling $-7.91 happened. Short-circuit now lives
            # at the top of the loop so no future code path can leak.
            if pos.module in ("M_reconciled", "M_reconciled_lowconf"):
                continue

            try:
                tick = await adapter.get_spot_ticker(sym)
                cur_price = float(tick["last"])
            except Exception:
                continue

            ret = (cur_price - pos.entry_price) / pos.entry_price
            pos.max_ret = max(pos.max_ret, ret)
            held_h = (time.time() - pos.entry_time) / 3600

            # Current scores for this symbol
            coin_data = rmap.get(sym, {})
            cur_spi = coin_data.get("spi", 0)

            # Recompute composite for this coin against live MIO
            if coin_data and mio:
                cur_composite = compute_composite_score(coin_data, mio)
            else:
                cur_composite = pos.composite_score  # fallback: assume unchanged

            # ── Hard exits (immediate, no hysteresis) ──
            reason = None
            if ret >= pos.tp:
                reason = "TP"
            elif ret <= pos.sl:
                reason = "SL"
            elif spi_mod.check_trailing_stop_tiered(
                ret, pos.max_ret, pos.tp,
                activate_frac=pos.trail_activate,
                trail_frac=pos.trail_pct,
            ):
                reason = "TRAIL"
            elif held_h >= pos.max_hold_h:
                reason = "BLITZ_TIMEOUT" if pos.tier == "A+" else "TIME_STOP"

            if reason:
                pos.composite_decay_hits = 0
                pos.spi_decay_hits = 0
                to_exit.append((sym, reason, ret))
                continue

            # ── Composite decay check (hysteresis: 2 consecutive hits) ──
            composite_floor = max(0.18, pos.composite_score * 0.55)
            if cur_composite < composite_floor:
                pos.composite_decay_hits += 1
            else:
                pos.composite_decay_hits = 0  # reset on recovery

            if pos.composite_decay_hits >= 2:
                to_exit.append((sym, "COMPOSITE_DECAY", ret))
                continue

            # ── SPI decay check (hysteresis: 3 consecutive hits + composite weakened) ──
            spi_floor = max(0.08, pos.entry_spi * 0.55)
            composite_soft_floor = pos.composite_score * 0.70
            if cur_spi < spi_floor and cur_composite < composite_soft_floor:
                pos.spi_decay_hits += 1
            else:
                pos.spi_decay_hits = 0  # reset on recovery

            if pos.spi_decay_hits >= 3:
                to_exit.append((sym, "SPI_DECAY_CONFIRMED", ret))
                continue

            # ── Swarm exit urgency (high urgency + held > 30 min) ──
            from .swarm.integration import swarm_exit_urgency
            urgency = swarm_exit_urgency(sym)
            if urgency >= 0.85 and held_h >= 0.5:
                to_exit.append((sym, "SWARM_EXIT", ret))
                continue

        for sym, reason, ret in to_exit:
            p = self.state.positions.get(sym)
            tier = p.tier if p else "?"
            age = (time.time() - p.entry_time) / 3600 if p else 0
            log.info("EXIT [%s] %s reason=%s ret=%.4f held=%.1fh", tier, sym, reason, ret, age)
            await self._close_position(sym, reason)

    async def _close_position(self, symbol: str, reason: str) -> None:
        pos = self.state.positions.get(symbol)
        if not pos:
            return
        # Record SL cooldown — prevent re-entry after stop-loss
        if reason in ("SL", "COMPOSITE_DECAY", "SPI_DECAY_CONFIRMED"):
            self._stoploss_cooldown_ts[symbol] = time.time()
        pnl = pos.size_usd * ((0 if self.dry_run else 1) *
              ((pos.max_ret if reason == "TRAIL" else 0) or 0))
        # Approximate PnL
        try:
            tick = await self._ensure_adapter().get_spot_ticker(symbol)
            cur = float(tick["last"])
            ret = (cur - pos.entry_price) / pos.entry_price
            pnl = pos.size_usd * ret
        except Exception:
            pnl = 0

        # Phase 11n-9-b: exits driven by engine safety rules (TP/SL/trail/
        # max_hold) are informational through the Pre-Trade Governor —
        # the governor RECORDS every exit but NEVER blocks a safety
        # exit (that would strand losing positions). Manual/alpha exits
        # go through authorize_trade() via a different code path.
        try:
            from spot_aggro.governance import pre_trade_gov
            pre_trade_gov.authorize_trade(
                symbol, "sell", getattr(pos, "tier", "?") or "?",
                source=f"engine_exit:{reason}",
            )
        except Exception:  # noqa: BLE001
            pass

        if not self.dry_run:
            # Phase 11n-9-kk: use place_market_verified_sell which reads
            # live OKX balance (not engine-tracked qty) and polls for
            # actual fill. If OKX has zero balance (position never
            # filled on entry OR already closed externally) the call
            # returns ok=False with explicit reason. In that case we
            # SKIP booking PnL because there's no real trade to book.
            try:
                adapter = self._ensure_adapter()
                exit_receipt = await adapter.place_market_verified_sell(
                    symbol=symbol, leg="spot",
                    idempotency_seed=f"sa-exit:{symbol}:{int(time.time()//60)}",
                )
                if not exit_receipt.ok:
                    log.warning(
                        "exit sell FAILED for %s: %s — skipping PnL book",
                        symbol, exit_receipt.error,
                    )
                    # Position stays in state; operator review required.
                    return
                # Re-compute realized PnL from actual fill avg_px vs entry.
                filled_qty = exit_receipt.filled_qty
                filled_avg_px = exit_receipt.avg_px
                filled_notional = filled_qty * filled_avg_px
                # Override pnl based on real fill.
                pnl = filled_notional - pos.size_usd
                fee_est = exit_receipt.fee_usd or (pos.size_usd * 0.001 * 2)
                log.info(
                    "exit VERIFIED %s: filled_qty=%.6f avg_px=%.6f notional=$%.4f pnl=$%+.4f",
                    symbol, filled_qty, filled_avg_px, filled_notional, pnl,
                )
                # Phase 11n-9-nn — A/B ledger close.
                try:
                    from spot_aggro.governance import live_variant_gate as _lvg_exit
                    _lvg_exit.record_variant_exit(
                        symbol=symbol, realized_pnl_usd=float(pnl),
                    )
                except Exception:
                    pass
            except Exception as exc:
                log.warning("spot sell failed for %s: %s — skipping PnL book", symbol, exc)
                return

        # Intraday compounding (spec §4)
        self.state.capital_usd += pnl
        if pnl > 0:
            self.state.peak_equity = max(self.state.peak_equity, self.state.capital_usd)

        # Fee estimate — OKX EEA spot taker is 0.10% per side. Real fee is
        # not returned by the bare market_order call. Tag fee_estimated=True
        # so forensic L1 can distinguish estimated vs reported fees.
        # Both sides (entry + exit) ⇒ 2 × notional × 0.001.
        fee_est = pos.size_usd * 0.001 * 2 if not self.dry_run else 0.0
        persist.log_trade(symbol=symbol, module=pos.module, action="exit",
                          side="sell", notional_usd=pos.size_usd,
                          fee_usd=fee_est, pnl_usd=pnl,
                          tier=pos.tier,
                          payload={"reason": reason, "ret": pos.max_ret,
                                   "entry_spi": pos.entry_spi,
                                   "is_blitz": pos.is_blitz,
                                   "tier": pos.tier,
                                   "regime": pos.entry_regime,
                                   "fee_estimated": True,
                                   "hold_seconds": int(time.time() - pos.entry_time)})
        try:
            from .telemetry import emit_trade as _emit_trade
            _emit_trade(
                "exit", symbol=symbol, tier=pos.tier, module=pos.module,
                notional_usd=pos.size_usd, fee_usd=fee_est, pnl_usd=pnl,
                reason=reason, composite=pos.composite_score,
                spi=pos.entry_spi,
            )
        except Exception:
            pass
        # Adaptive-universe: feed realised outcome into (symbol, tier, regime)
        # memory. Never raises — any DB issue is logged and swallowed so an
        # exit path can't be blocked by telemetry.
        try:
            coin_memory.record_exit(
                symbol=symbol, tier=pos.tier,
                regime=pos.entry_regime or "UNKNOWN",
                pnl_usd=float(pnl),
            )
        except Exception:
            log.exception("coin_memory.record_exit failed (non-fatal)")
        # Phase 11n-9-ee: mirror live PnL into every shadow variant that
        # admitted this entry. Pure telemetry — never blocks.
        try:
            if getattr(pos, "authz_id", ""):
                from spot_aggro.governance.three_way_shadow import (
                    record_exit as _tw_record_exit,
                )
                _tw_record_exit(
                    correlation_id=pos.authz_id,
                    symbol=symbol,
                    tier=pos.tier,
                    pnl_usd=float(pnl),
                    fee_usd=float(fee_est),
                    notional_usd=float(pos.size_usd),
                    payload={"reason": reason, "module": pos.module},
                )
        except Exception:
            pass
        # Phase 11n-9-hh — append exit to hash-chained immutable ledger.
        try:
            from spot_aggro.governance.immutable_ledger import append as _il_append
            _il_append(
                "exit", symbol=symbol, tier=pos.tier,
                notional_usd=float(pos.size_usd),
                pnl_usd=float(pnl), fee_usd=float(fee_est),
                correlation_id=str(getattr(pos, "authz_id", "") or ""),
                payload={
                    "module": pos.module, "reason": reason,
                    "hold_seconds": int(time.time() - pos.entry_time),
                },
            )
        except Exception:
            pass
        notify.pair_exit(symbol=symbol, module=pos.module, reason=reason, pnl_usd=pnl)

        if pos.is_blitz:
            self.state.blitz_active = False
        del self.state.positions[symbol]

    # ------------------------------------------------------------------
    # BLITZ MODE (spec §3)
    # ------------------------------------------------------------------

    async def _check_blitz(self, adapter: OKXUnified, rankings: list[dict]) -> None:
        if not rankings:
            return
        if self.state.blitz_active:
            return
        if self.state.blitz_count_24h >= BLITZ_DAILY_CAP:
            return
        if time.time() - self.state.last_blitz_time < BLITZ_COOLDOWN_S:
            return

        best = rankings[0]
        if best["spi"] < BLITZ_SPI_MIN:
            return

        # Phase 11n-9-bb: swarm prefilter (BLITZ).
        try:
            from .swarm.prefilter import should_call_llm
            from spot_aggro.governance.universe_gatekeeper import is_cell_admitted
            allow_llm, reason = should_call_llm(best["symbol"], layer="blitz")
            if not allow_llm:
                log.info("BLITZ consensus skipped: %s", reason)
                return
            if not is_cell_admitted("tier_symbol", f"A+|{best['symbol']}"):
                log.info("BLITZ consensus skipped: A+|%s not admitted",
                         best["symbol"])
                return
        except Exception:
            return

        # Tighter consensus for BLITZ
        ctx = {**best, "ret_24h": 0, "ret_7d": 0, "oi_usd": 0, "oi_change": 0,
               "spi_fz": best["components"]["fz"], "spi_oi": best["components"]["oi"],
               "spi_div": best["components"]["div"], "spi_liq": best["components"]["liq"],
               "liq_1h": 0, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
               "funding_sigma": best["sigma_30d"] * 100}
        c = await llm_consensus.run_consensus(symbol=best["symbol"], ctx=ctx)
        if c.vetoed or c.consensus < 0.55 or c.conflict > 0.60:
            return

        blitz_size = self.state.capital_usd * BLITZ_ALLOC
        tp = 0.025 + 0.025 * best["spi"]   # 2.5% to 4.75%
        sl = -0.012                          # fixed -1.2%

        # Phase D2α — BLITZ pre-flight guard (mirrors the tiered path).
        try:
            free_usdt = await adapter.get_free_usdt()
        except Exception:
            free_usdt = None
        if free_usdt is not None and blitz_size > free_usdt:
            reason_code = (
                "INSUFFICIENT_FREE_USDT" if free_usdt < 1.0
                else "SKIPPED_NO_BUYING_POWER"
            )
            log.info(
                "BLITZ pre-flight SKIP %s: size $%.2f > free_usdt $%.4f (%s)",
                best["symbol"], blitz_size, free_usdt, reason_code,
            )
            persist.log_trade(
                symbol=best["symbol"], module="M3_blitz", action="skip",
                tier="A+",
                payload={
                    "reason": reason_code,
                    "free_usdt": round(free_usdt, 4),
                    "required_size": round(blitz_size, 4),
                    "tier": "A+",
                    "composite": best.get("composite"),
                    "note": "not sent to OKX — execution sufficiency guard (BLITZ)",
                },
            )
            return

        # Phase 11n-9-aa: Trade Readiness gate (BLITZ).
        try:
            from spot_aggro.governance.trade_readiness import is_ready_to_trade
            if not is_ready_to_trade():
                log.info("BLITZ blocked: not_ready_to_trade")
                return
        except Exception:  # noqa: BLE001
            log.exception("trade_readiness probe fault — BLITZ blocked")
            return

        # Phase 11n-9-aa: Universe Gatekeeper (BLITZ is A+; current seed
        # is Tier-C + {ENA,DOT} so BLITZ is effectively blocked during
        # the initial post-promotion runtime until a Tier-A+ cell for
        # the picked symbol earns admission).
        try:
            from spot_aggro.governance.universe_gatekeeper import is_cell_admitted
            blitz_cell_key = f"A+|{best['symbol']}"
            if not is_cell_admitted("tier_symbol", blitz_cell_key):
                log.info("BLITZ blocked: cell_not_admitted (%s)", blitz_cell_key)
                return
        except Exception:  # noqa: BLE001
            log.exception("universe_gatekeeper probe fault — BLITZ blocked")
            return

        # Phase 11n-9-y: Layer 3 Contradiction Freeze gate (BLITZ).
        try:
            from spot_aggro.governance.contradiction_freeze import is_entry_frozen
            if is_entry_frozen():
                log.info("BLITZ blocked: contradiction_freeze_active")
                return
        except Exception:  # noqa: BLE001
            log.exception("contradiction_freeze probe fault — BLITZ blocked")
            return

        # Phase 11n-9-b: Pre-Trade Governor gates blitz buys too.
        try:
            from spot_aggro.governance import pre_trade_gov
            authz = pre_trade_gov.authorize_trade(
                best["symbol"], "buy", "A+",
                source="engine_entry:M3_blitz",
            )
            if not authz.passed:
                log.info("BLITZ blocked by pre_trade_gov: %s",
                         authz.rejection_reason)
                return
        except Exception:  # noqa: BLE001
            log.exception("pre_trade_gov fault — BLITZ blocked")
            return

        receipt = await adapter.place_post_only(
            symbol=best["symbol"], side="buy", notional_usd=blitz_size,
            reference_price=best["price"], leg="spot",
            idempotency_seed=f"blitz:{best['symbol']}:{int(time.time())}",
        )
        if not receipt.ok:
            return

        pos = Position(
            symbol=best["symbol"], side="buy", size_usd=blitz_size,
            entry_price=best["price"], entry_spi=best["spi"],
            entry_time=time.time(), tp=tp, sl=sl, is_blitz=True,
            module="M3_blitz",
        )
        self.state.positions[best["symbol"]] = pos
        self.state.blitz_active = True
        self.state.blitz_count_24h += 1
        self.state.last_blitz_time = time.time()

        try:
            from .swarm.runner import get_coin_intel
            ci = get_coin_intel(best["symbol"])
        except Exception:
            ci = None
        persist.log_trade(symbol=best["symbol"], module="M3_blitz", action="enter",
                          side="buy", notional_usd=blitz_size, avg_px=best["price"],
                          tier="A+",
                          payload={
                              "blitz": True, "is_blitz": True,
                              "spi": best["spi"], "tp": tp, "sl": sl,
                              "tier": "A+",  # BLITZ is the strongest tier
                              "composite": best.get("composite"),
                              "consensus": c.consensus,
                              # Forensic snapshot — BLITZ doesn't have mio in
                              # scope; entry_regime captured as UNKNOWN.
                              "entry_regime": "UNKNOWN",
                              "composite_score": best.get("composite"),
                              "swarm_action_at_entry":
                                  getattr(ci, "final_action", None) if ci else None,
                              "swarm_confidence_at_entry":
                                  getattr(ci, "buy_confidence", None) if ci else None,
                              "swarm_agents_ok_at_entry":
                                  getattr(ci, "agents_ok", None) if ci else None,
                              "swarm_agents_total_at_entry":
                                  getattr(ci, "agents_total", None) if ci else None,
                              "swarm_layer_at_entry":
                                  getattr(ci, "layer", None) if ci else None,
                          })
        notify.pair_enter(symbol=best["symbol"], module="M3_blitz",
                          notional_usd=blitz_size, side="buy",
                          consensus=c.consensus, abs_z=abs(best["funding_z"]))
        log.warning("BLITZ FIRED: %s $%.2f spi=%.3f", best["symbol"], blitz_size, best["spi"])

    # ------------------------------------------------------------------
    # DD level management (spec §8.2)
    # ------------------------------------------------------------------

    def _update_dd_level(self) -> None:
        if self.state.peak_equity <= 0:
            return
        dd = (self.state.peak_equity - self.state.current_equity) / self.state.peak_equity
        if dd > KILL_DD_PCT:
            self.state.dd_level = 3
        elif dd > 0.05:
            self.state.dd_level = 2
        elif dd > 0.03:
            self.state.dd_level = 1
        else:
            self.state.dd_level = 0

    async def _update_equity(self) -> None:
        if self.dry_run:
            return
        try:
            eq = await self._ensure_adapter().get_account_equity()
            self.state.current_equity = eq
            if eq > self.state.peak_equity:
                self.state.peak_equity = eq
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        from .research.runner import get_mio
        mio = get_mio()
        # Phase 11n-7: enrich position payload with LIVE unrealized PnL.
        # For scored positions max_ret is updated in _check_exits_v2, but
        # reconciled/low-conf positions short-circuit that loop (by
        # design — they must not auto-exit). That leaves their displayed
        # PnL stuck at 0.00%. Fetch current ticker once per status call
        # and compute live_ret for display only. Falls back to max_ret
        # if the ticker is unreachable.
        live_prices = self._snapshot_live_prices(
            list(self.state.positions.keys())
        )
        return {
            "engine": "spot_aggro",
            "mode": "paper" if self.dry_run else "live",
            "cycles": self.state.cycles,
            "capital_usd": self.state.capital_usd,
            "peak_equity": self.state.peak_equity,
            "current_equity": self.state.current_equity,
            "dd_level": self.state.dd_level,
            "halted": self.state.halted,
            "blitz_active": self.state.blitz_active,
            "blitz_count_24h": self.state.blitz_count_24h,
            "trades_today": self.state.trades_today,
            "positions": {
                sym: {
                    "size_usd": p.size_usd, "entry_price": p.entry_price,
                    "entry_spi": p.entry_spi, "tp": p.tp, "sl": p.sl,
                    "max_ret": p.max_ret, "is_blitz": p.is_blitz,
                    "age_h": (time.time() - p.entry_time) / 3600,
                    "tier": p.tier,
                    "composite": p.composite_score,
                    "module": p.module,
                    "max_hold_h": p.max_hold_h,
                    # Live unrealized PnL for DISPLAY only — never feeds
                    # exit decisions. None when ticker unreachable.
                    "live_price": live_prices.get(sym),
                    "live_ret": (
                        (live_prices[sym] - p.entry_price) / p.entry_price
                        if (live_prices.get(sym) is not None
                            and p.entry_price > 0)
                        else None
                    ),
                }
                for sym, p in self.state.positions.items()
            },
            # MIO (30-min research intelligence)
            "mio": {
                "regime": mio.regime,
                "regime_confidence": mio.regime_confidence,
                "squeeze_timing": mio.squeeze_timing_window,
                "edge_status": mio.edge_status,
                "universe_quality": mio.universe_quality,
                "spi_threshold_adj": mio.spi_threshold_adj,
                "tp_multiplier": mio.tp_multiplier,
                "sl_multiplier": mio.sl_multiplier,
                "position_scale": mio.recommended_position_scale,
                "blitz_readiness": mio.blitz_readiness,
                "top_6": mio.top_6_assets,
                "asset_scores": mio.asset_scores,
                "events": mio.events_next_24h[:3],
                "cycle_number": mio.cycle_number,
                "last_updated": mio.timestamp,
            },
            # Effective gates (from MIO + base)
            "effective_spi_min": getattr(self, '_effective_spi_min', SPI_MIN),
            "effective_consensus_min": CONSENSUS_MIN,
            "effective_conflict_max": CONFLICT_MAX,
            # v2: Tier system
            "tier_system": {
                "version": "v2.0",
                "frequency": self.state.frequency_ctrl.state_dict(),
                "tiered_universe": [
                    {"symbol": r["symbol"], "spi": r["spi"],
                     "composite": r.get("composite", 0),
                     "tier": r.get("tier", "?"),
                     "funding_z": r["funding_z"],
                     "depth_usd": r["depth_usd"]}
                    for r, _tc in getattr(self, '_tiered_cache', [])[:10]
                ],
            },
            # 5-LLM Swarm intelligence
            "swarm": self._get_swarm_summary(),
            # Rankings snapshot (backward compat)
            "rankings": [
                {"symbol": r["symbol"], "spi": r["spi"],
                 "funding_z": r["funding_z"], "depth_usd": r["depth_usd"]}
                for r in (self._rank_cache or [])[:10]
            ],
            # Phase D2β — reconciliation truth. Tracked vs untracked.
            # Dashboard binds directly to these fields. None means no
            # reconciliation has run yet (e.g. dry_run or startup failed).
            "reconciliation": self.state.reconciliation_summary,
            # True iff the engine holds real positions on the exchange
            # that are NOT reflected in state.positions. Dashboard uses
            # this to fire the BLIND-STATE warning.
            "blind_state": (
                bool(self.state.reconciliation_summary and
                     self.state.reconciliation_summary.get("n_pending", 0) > 0)
            ),
        }

    def _get_swarm_summary(self) -> dict:
        try:
            from .swarm.integration import swarm_summary_for_status
            return swarm_summary_for_status()
        except Exception:
            return {"active": False}


# ---------------------------------------------------------------------------
# Backward-compatibility alias.
#
# Phase 11g renamed ``APEX_Spot_Aggro`` -> ``SpotAggroEngine`` so the class
# name no longer implies apex-engine ownership. The old name is retained
# as a module-level alias so any external caller that still imports
# ``from spot_aggro.engine import APEX_Spot_Aggro`` keeps working while
# callers migrate. All NEW code must use ``SpotAggroEngine``. Remove this
# alias once a full-repo grep confirms no remaining callers.
# ---------------------------------------------------------------------------
APEX_Spot_Aggro = SpotAggroEngine
