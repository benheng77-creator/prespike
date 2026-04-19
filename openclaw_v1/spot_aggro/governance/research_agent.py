"""Phase 11l/11m — SPOT AGGRO Win-Rate Research Agent (deterministic mode).

Hourly autonomous research pass that:
  1. Computes per-tier win rate across 30m / 1h / 24h rolling windows
     from CLOSED realised outcomes only.
  2. Computes Wilson 95% CI for each window's WR.
  3. Applies soft-halt / restore decisions using the **1h window** as
     the primary gate (configurable) with MIN_SAMPLE_SIZE guard and
     hysteresis (halt ≤ halt_min · restore ≥ restore_min).
  4. Persists a structured report with snapshot_id + audit_rollup_id
     evidence linkage to `spot_research_reports`.
  5. On halt: flips the tier toggle OFF and asks the adapter to cancel
     any pending buys for that tier. Open positions are NEVER touched.
     On restore: flips the toggle back ON.
  6. Supports deterministic test mode via injected clock. No wall-clock
     dependency in CI.

Entry points:
  - `run_research(clock=?, window_mode="live")` -> ResearchReport
  - `run_and_persist(clock=?, ...)` -> ResearchReport (stores result)
  - `latest_report()` / `history(start?, end?, tier?)`

CLI:
  python -m openclaw_v1.spot_aggro.governance.research_agent run_once \
     [--simulate-time ISO] [--window-hours 24]

SPOT AGGRO only. No apex_omega imports. Read-only against trading
state except the tier-toggle flip, which is the authorised control
surface. Reconciled modules (M_reconciled, M_reconciled_lowconf) are
excluded from canonical-tier WR because they are cleanup, not strategy.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Clock provider — deterministic test mode.
# ---------------------------------------------------------------------------

Clock = Callable[[], float]  # returns seconds-since-epoch


def _wall_clock() -> float:
    return time.time()


# Module-level default clock. Tests/CLI override via run_research(clock=...)
# or by setting the DET_CLOCK env + _DET_CLOCK_AT global; never monkeypatch
# time.time() directly.
_DEFAULT_CLOCK: Clock = _wall_clock


def set_default_clock(clock: Clock) -> None:
    """Install a module-level default clock. Used by the CLI when
    --simulate-time is provided."""
    global _DEFAULT_CLOCK
    _DEFAULT_CLOCK = clock


# ---------------------------------------------------------------------------
# Config — env-override chain matching the rest of the repo.
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, "").strip())
        if math.isfinite(v) and v > 0:
            return v
    except (ValueError, TypeError):
        pass
    return default


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "").strip() or default))
    except (ValueError, TypeError):
        return default


def _load_yaml_config() -> dict[str, Any]:
    """Merge defaults with openclaw_v1/spot_aggro/config/research.yml if
    present. Env overrides always win."""
    defaults = {
        "wr_halt_min": 0.60,
        "wr_resume_min": 0.50,       # (kept for backward compat with 11l)
        "wr_restore_min": 0.50,       # 11m canonical name
        "min_sample_size": 50,
        "primary_window": "1h",       # "30m" | "1h" | "24h"
        "staged_sizing": 0.5,
        # Phase 11n-9-g: added 7d window so the dashboard tier tile
        # shows a meaningful WR across the coin history, not just the
        # live 1h gate window (which reads 0 on quiet days).
        "windows_hours": {"30m": 0.5, "1h": 1.0, "24h": 24.0, "7d": 168.0},
    }
    try:
        import yaml
    except ImportError:
        return defaults
    cfg_path = (
        Path(__file__).resolve().parent.parent
        / "config" / "research.yml"
    )
    if not cfg_path.exists():
        return defaults
    try:
        loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        defaults.update({k: v for k, v in loaded.items() if v is not None})
    except Exception:  # noqa: BLE001
        pass
    return defaults


_CFG = _load_yaml_config()

WR_HALT_MIN = _env_float("SPOT_RESEARCH_WR_HALT_MIN", float(_CFG["wr_halt_min"]))
# Canonical name (Phase 11m); env var SPOT_RESEARCH_WR_RESTORE_MIN wins.
# Back-compat: if legacy SPOT_RESEARCH_WR_RESUME_MIN is set and the new
# one isn't, use the legacy value. Otherwise use YAML default.
_legacy_resume = _env_float(
    "SPOT_RESEARCH_WR_RESUME_MIN",
    float(_CFG.get("wr_resume_min", _CFG.get("wr_restore_min", 0.5))),
)
WR_RESTORE_MIN = _env_float("SPOT_RESEARCH_WR_RESTORE_MIN", _legacy_resume)
# Phase 11l tests read WR_RESUME_MIN; keep it in sync with the canonical.
WR_RESUME_MIN = WR_RESTORE_MIN
MIN_SAMPLE_FOR_HALT = _env_int("SPOT_RESEARCH_MIN_SAMPLE", int(_CFG["min_sample_size"]))
PRIMARY_WINDOW = os.environ.get("SPOT_RESEARCH_PRIMARY_WINDOW",
                                str(_CFG["primary_window"])).strip() or "1h"

# Windows we compute and expose; primary drives halt/restore.
WINDOWS_HOURS = {
    "30m": float(_CFG.get("windows_hours", {}).get("30m", 0.5)),
    "1h":  float(_CFG.get("windows_hours", {}).get("1h",  1.0)),
    "24h": float(_CFG.get("windows_hours", {}).get("24h", 24.0)),
    "7d":  float(_CFG.get("windows_hours", {}).get("7d", 168.0)),
}

SPOT_MODULE_LIKE = (
    "M1_squeeze%", "M1_flow%", "M1_scalp%", "M3_blitz%",
)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class WilsonCI:
    """Wilson score interval for a binomial proportion at 95% CI."""
    low: float
    high: float
    width: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass
class WindowStats:
    """Per-tier stats in one window (30m / 1h / 24h)."""
    window: str                    # "30m" | "1h" | "24h"
    n_enters: int = 0
    n_exits: int = 0
    n_wins: int = 0
    n_losses: int = 0
    total_pnl_usd: float = 0.0
    win_rate: Optional[float] = None
    confidence_interval_95: Optional[WilsonCI] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.confidence_interval_95 is not None:
            d["confidence_interval_95"] = self.confidence_interval_95.to_dict()
        return d


@dataclass
class TierStats:
    tier: str
    windows: dict[str, WindowStats] = field(default_factory=dict)
    # Primary-window shortcuts (what the halt gate reads):
    primary_window: str = "1h"
    primary_wr: Optional[float] = None
    primary_sample: int = 0
    primary_ci: Optional[WilsonCI] = None
    # Legacy Phase 11l fields (retained so old tests + dashboard bindings
    # keep working). These mirror primary_window.
    n_enters: int = 0
    n_exits: int = 0
    n_wins: int = 0
    n_losses: int = 0
    total_pnl_usd: float = 0.0
    win_rate: Optional[float] = None
    avg_pnl_usd: Optional[float] = None
    halt_verdict: str = "allow"    # allow | halt | thaw | insufficient_sample
    halt_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = {
            "tier": self.tier,
            "primary_window": self.primary_window,
            "primary_wr": self.primary_wr,
            "primary_sample": self.primary_sample,
            "primary_ci": self.primary_ci.to_dict() if self.primary_ci else None,
            "windows": {k: v.to_dict() for k, v in self.windows.items()},
            "n_enters": self.n_enters, "n_exits": self.n_exits,
            "n_wins": self.n_wins, "n_losses": self.n_losses,
            "total_pnl_usd": self.total_pnl_usd,
            "win_rate": self.win_rate, "avg_pnl_usd": self.avg_pnl_usd,
            "halt_verdict": self.halt_verdict,
            "halt_reason": self.halt_reason,
        }
        return d


@dataclass
class Recommendation:
    severity: str          # "info" | "warn" | "action"
    category: str          # "tier" | "symbol" | "regime" | "timing" | "data"
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class CancelAttempt:
    tier: str
    attempted_at_ms: int
    cancelled_count: int
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResearchReport:
    # Identity + evidence linkage.
    report_id: str
    snapshot_id: str              # new Phase 11m
    audit_rollup_id: str          # new Phase 11m
    generated_ts_ms: int
    status: str                   # "interim" | "final"
    window_h: int                 # window used for the legacy single-window stats

    # Multi-window aggregate stats.
    tier_stats: list[TierStats]
    overall_wr: Optional[float]           # primary-window aggregate
    overall_exits: int
    overall_pnl_usd: float

    # Halt / restore.
    halt_state: dict[str, bool]           # tier -> is_halted
    cancel_attempts: list[CancelAttempt]

    # Narrative + evidence.
    recommendations: list[Recommendation]
    experiments: list[dict[str, Any]]
    research_notes: list[str]
    evidence_refs: list[str]
    thresholds: dict[str, Any]
    # Phase 11n-2: per-symbol quality (Coin Accuracy Matrix folded in).
    per_symbol: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "snapshot_id": self.snapshot_id,
            "audit_rollup_id": self.audit_rollup_id,
            "timestamp": datetime.fromtimestamp(
                self.generated_ts_ms / 1000, tz=timezone.utc
            ).isoformat(),
            "generated_ts_ms": self.generated_ts_ms,
            "status": self.status,
            "window_h": self.window_h,
            "tier_stats": [s.to_dict() for s in self.tier_stats],
            "overall_wr": self.overall_wr,
            "overall_exits": self.overall_exits,
            "overall_pnl_usd": self.overall_pnl_usd,
            "halt_state": self.halt_state,
            "cancel_attempts": [
                c.to_dict() if hasattr(c, "to_dict") else dict(c)
                for c in self.cancel_attempts
            ],
            "recommendations": [asdict(r) for r in self.recommendations],
            "experiments": list(self.experiments),
            "research_notes": list(self.research_notes),
            "evidence_refs": list(self.evidence_refs),
            "thresholds": dict(self.thresholds),
            "per_symbol": list(self.per_symbol),
        }


# ---------------------------------------------------------------------------
# Wilson score 95% CI.
# ---------------------------------------------------------------------------

def wilson_ci_95(wins: int, total: int) -> Optional[WilsonCI]:
    """Wilson score interval for a binomial proportion at 95% CI.

    Returns None when `total` is 0. Formula:
      p = wins/total
      z = 1.959963984540054 (two-sided 95%)
      denom = 1 + z²/n
      center = (p + z²/(2n)) / denom
      margin = z * sqrt(p(1-p)/n + z²/(4n²)) / denom
      low  = center - margin
      high = center + margin
    Width = high - low (useful for ranking stats by uncertainty).
    """
    if total <= 0:
        return None
    z = 1.959963984540054
    p = wins / total
    z2 = z * z
    denom = 1.0 + z2 / total
    center = (p + z2 / (2.0 * total)) / denom
    margin_inner = p * (1.0 - p) / total + z2 / (4.0 * total * total)
    margin = z * math.sqrt(margin_inner) / denom
    low = max(0.0, center - margin)
    high = min(1.0, center + margin)
    # Float-precision pin: when wins == total, the algebraic upper bound is
    # exactly 1.0 but IEEE-754 leaves ~1e-16 slop. Snap so callers don't
    # have to do epsilon-checks.
    if wins == total and high > 0.9999999999: high = 1.0
    if wins == 0 and low < 1e-10: low = 0.0
    return WilsonCI(low=low, high=high, width=high - low)


# ---------------------------------------------------------------------------
# DB access.
# ---------------------------------------------------------------------------

def _spot_module_where_clause() -> str:
    parts = [f"module LIKE '{p}'" for p in SPOT_MODULE_LIKE]
    return "(" + " OR ".join(parts) + ")"


def _fetch_window_stats(
    now_s: float, window_h: float, clock: Clock
) -> dict[str, WindowStats]:
    """Return per-tier WindowStats for the [now - window_h, now] window."""
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    label = _label_for_hours(window_h)
    try:
        cutoff_ms = int((now_s - window_h * 3600) * 1000)
        rows = con.execute(
            f"SELECT tier, action, pnl_usd FROM trade_log "
            f"WHERE ts_ms > ? AND tier IN ('A+','A','B','C') "
            f"AND {_spot_module_where_clause()}",
            (cutoff_ms,),
        ).fetchall()
    finally:
        con.close()

    buckets = {t: WindowStats(window=label) for t in ("A+", "A", "B", "C")}
    for tier, action, pnl_usd in rows:
        s = buckets[tier]
        if action == "enter":
            s.n_enters += 1
        elif action == "exit":
            s.n_exits += 1
            pnl = float(pnl_usd) if pnl_usd is not None else 0.0
            s.total_pnl_usd += pnl
            if pnl > 0.001:
                s.n_wins += 1
            elif pnl < -0.001:
                s.n_losses += 1
    for s in buckets.values():
        if s.n_exits > 0:
            s.win_rate = s.n_wins / s.n_exits
            s.confidence_interval_95 = wilson_ci_95(s.n_wins, s.n_exits)
    return buckets


def _label_for_hours(h: float) -> str:
    if abs(h - 0.5) < 1e-9: return "30m"
    if abs(h - 1.0) < 1e-9: return "1h"
    if abs(h - 24.0) < 1e-9: return "24h"
    return f"{h}h"


def _fetch_per_symbol(now_s: float, window_h: float) -> list[dict[str, Any]]:
    """Phase 11n-9-g: per-symbol stats use a WIDER window than the primary
    halt window so the coin-quality signal survives quiet periods. The
    dashboard's Coin Accuracy Matrix uses 7d; we match that here so the
    two cards never disagree. Overridable via
    SPOT_RESEARCH_PER_SYMBOL_HOURS (default 168h = 7d).
    """
    from shared.persistence import state as persist
    persist.init_schema()
    try:
        per_sym_hours = float(os.environ.get(
            "SPOT_RESEARCH_PER_SYMBOL_HOURS", "").strip() or "168")
    except (ValueError, TypeError):
        per_sym_hours = 168.0
    effective_hours = max(window_h, per_sym_hours)
    con = persist._connect()
    try:
        cutoff_ms = int((now_s - effective_hours * 3600) * 1000)
        rows = con.execute(
            f"SELECT symbol, tier, "
            f"  sum(CASE WHEN action='exit' THEN 1 ELSE 0 END) as exits, "
            f"  sum(CASE WHEN action='exit' AND pnl_usd > 0.001 THEN 1 ELSE 0 END) as wins, "
            f"  sum(CASE WHEN action='exit' AND pnl_usd < -0.001 THEN 1 ELSE 0 END) as losses, "
            f"  sum(CASE WHEN action='exit' THEN COALESCE(pnl_usd,0) ELSE 0 END) as pnl "
            f"FROM trade_log "
            f"WHERE ts_ms > ? AND tier IN ('A+','A','B','C') "
            f"AND {_spot_module_where_clause()} "
            f"GROUP BY symbol, tier HAVING exits > 0 ORDER BY pnl ASC",
            (cutoff_ms,),
        ).fetchall()
    finally:
        con.close()
    return [
        {"symbol": r[0], "tier": r[1], "exits": int(r[2]),
         "wins": int(r[3]), "losses": int(r[4]), "pnl_usd": float(r[5]),
         "win_rate": (int(r[3]) / int(r[2])) if r[2] else None}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Tier-toggle integration.
# ---------------------------------------------------------------------------

def _get_toggle():
    try:
        from spot_aggro.api import routes as spot_routes
        if hasattr(spot_routes, "_spot_tier_toggle"):
            return spot_routes._spot_tier_toggle()
        if getattr(spot_routes, "_SPOT_TIER_TOGGLE", None) is not None:
            return spot_routes._SPOT_TIER_TOGGLE
    except Exception:  # noqa: BLE001
        pass
    from spot_aggro.gates.tier_toggle import TierExecutionToggle
    return TierExecutionToggle()


def _current_toggle_snapshot() -> dict[str, bool]:
    try:
        return dict(_get_toggle().snapshot())
    except Exception:  # noqa: BLE001
        return {}


def _flip_toggle(tier: str, enabled: bool, reason: str) -> bool:
    try:
        _get_toggle().set_enabled(
            tier, enabled,
            actor="research_agent", note=reason[:200], persist=True,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Order cancellation on halt.
# ---------------------------------------------------------------------------

async def _cancel_buys_for_tier_async(tier: str) -> CancelAttempt:
    """Cancel any pending BUYS for the tier. Open positions are NOT
    touched. Never raises — failures are recorded and returned."""
    from shared.adapters.okx_unified import OKXUnified
    attempt = CancelAttempt(
        tier=tier,
        attempted_at_ms=int(time.time() * 1000),
        cancelled_count=0,
    )
    try:
        adapter = OKXUnified(engine="spot_aggro")
    except Exception as exc:  # noqa: BLE001
        attempt.errors.append(f"adapter_init: {type(exc).__name__}: {exc}")
        return attempt

    try:
        if hasattr(adapter, "cancel_open_buys_for_tier"):
            res = await adapter.cancel_open_buys_for_tier(tier)
            attempt.cancelled_count = int(res.get("cancelled", 0))
            attempt.errors.extend(list(res.get("errors", []))[:5])
        else:
            attempt.errors.append(
                "adapter missing cancel_open_buys_for_tier — no cancellation possible"
            )
    except Exception as exc:  # noqa: BLE001
        attempt.errors.append(f"cancel: {type(exc).__name__}: {str(exc)[:120]}")
    return attempt


def _cancel_buys_for_tier(tier: str) -> CancelAttempt:
    """Sync wrapper. Spins up / reuses an event loop. Normalises any
    return type into a CancelAttempt (the async side may be mocked in
    tests to return a plain dict)."""
    import asyncio
    try:
        loop = asyncio.new_event_loop()
        try:
            raw = loop.run_until_complete(_cancel_buys_for_tier_async(tier))
        finally:
            loop.close()
    except Exception as exc:  # noqa: BLE001
        return CancelAttempt(
            tier=tier,
            attempted_at_ms=int(time.time() * 1000),
            cancelled_count=0,
            errors=[f"loop: {type(exc).__name__}: {str(exc)[:120]}"],
        )
    # Normalise dict-like returns (mocks) into CancelAttempt.
    if isinstance(raw, CancelAttempt):
        return raw
    if isinstance(raw, dict):
        return CancelAttempt(
            tier=raw.get("tier", tier),
            attempted_at_ms=int(raw.get("attempted_at_ms", time.time() * 1000)),
            cancelled_count=int(raw.get("cancelled", raw.get("cancelled_count", 0))),
            errors=list(raw.get("errors", []) or []),
        )
    return CancelAttempt(
        tier=tier,
        attempted_at_ms=int(time.time() * 1000),
        cancelled_count=0,
        errors=[f"unexpected return type: {type(raw).__name__}"],
    )


# ---------------------------------------------------------------------------
# Core analysis.
# ---------------------------------------------------------------------------

def _decide_halt_verdict(s: TierStats) -> TierStats:
    """Per-tier halt decision using PRIMARY window with sample-size +
    hysteresis guards."""
    sample = s.primary_sample
    wr = s.primary_wr

    if sample < MIN_SAMPLE_FOR_HALT:
        s.halt_verdict = "insufficient_sample"
        s.halt_reason = (
            f"only {sample} closed exits in {s.primary_window}; "
            f"need {MIN_SAMPLE_FOR_HALT}+"
        )
        return s

    if wr is None:
        s.halt_verdict = "insufficient_sample"
        s.halt_reason = f"no wr computable for {s.primary_window}"
        return s

    toggles = _current_toggle_snapshot()
    currently_on = toggles.get(s.tier, True)

    if wr < WR_HALT_MIN:
        s.halt_verdict = "halt"
        s.halt_reason = (
            f"{s.primary_window} WR {wr*100:.1f}% < halt threshold "
            f"{WR_HALT_MIN*100:.0f}% across {sample} exits"
        )
    elif (not currently_on) and wr >= WR_RESTORE_MIN:
        s.halt_verdict = "thaw"
        s.halt_reason = (
            f"{s.primary_window} WR {wr*100:.1f}% >= restore threshold "
            f"{WR_RESTORE_MIN*100:.0f}% across {sample} exits; lifting halt"
        )
    else:
        s.halt_verdict = "allow"
        s.halt_reason = f"{s.primary_window} WR {wr*100:.1f}% within tolerance"
    return s


def _enforce_halt(tier_stats: list[TierStats]) -> tuple[dict[str, bool], list[CancelAttempt]]:
    """Phase 11n-2: by default the research agent is ADVISORY only —
    it publishes verdicts and targets a system-wide WR ≥ halt_min but
    never flips tier toggles or cancels buys on its own. Operator action
    (or the opt-in SPOT_RESEARCH_ENFORCE_HALT=1 flag) is required to
    translate a `halt` verdict into an actual execution-lane change.

    This removes the "60% per-tier hard gate on trades" behavior per
    operator decision 2026-04-19 (Phase 11n-2 Q4). The research loop
    still runs non-stop; the scenario lab still searches for recovery
    configurations; the truth governor still tags reports. What it no
    longer does is silently block Tier B trades because Tier B's WR
    dipped in a rolling window.
    """
    enforce = os.environ.get("SPOT_RESEARCH_ENFORCE_HALT", "0").strip() == "1"
    cancels: list[CancelAttempt] = []
    toggles_after: dict[str, bool] = {}
    for s in tier_stats:
        before_on = _current_toggle_snapshot().get(s.tier, True)
        if enforce:
            if s.halt_verdict == "halt" and before_on:
                _flip_toggle(s.tier, False, s.halt_reason)
                cancels.append(_cancel_buys_for_tier(s.tier))
            elif s.halt_verdict == "thaw" and not before_on:
                _flip_toggle(s.tier, True, s.halt_reason)
        after_on = _current_toggle_snapshot().get(s.tier, True)
        toggles_after[s.tier] = not after_on
    return toggles_after, cancels


def _build_recommendations(
    tier_stats: list[TierStats],
    per_symbol: list[dict[str, Any]],
) -> tuple[list[Recommendation], list[dict[str, Any]]]:
    recs: list[Recommendation] = []
    experiments: list[dict[str, Any]] = []

    for s in tier_stats:
        if s.halt_verdict == "halt":
            recs.append(Recommendation(
                severity="action", category="tier",
                message=f"Tier {s.tier} soft-halted — {s.halt_reason}",
                evidence={"tier": s.tier, "win_rate": s.primary_wr,
                          "sample": s.primary_sample, "pnl": s.total_pnl_usd},
            ))
            experiments.append({
                "experiment_id": f"exp-tune-{s.tier.lower()}-{int(time.time())}",
                "hypothesis": (
                    f"Tier {s.tier} TP/SL ratio is misaligned for current "
                    f"regime; tightening TP to 0.8× and loosening SL by 1.2× "
                    f"may invert the W/L distribution."
                ),
                "expected_impact_pct": 5.0,
                "safe_rollout": "staged_sizing",
                "staged_sizing": float(_CFG.get("staged_sizing", 0.5)),
                "tier": s.tier,
            })
        elif s.halt_verdict == "thaw":
            recs.append(Recommendation(
                severity="info", category="tier",
                message=f"Tier {s.tier} thawed — {s.halt_reason}",
                evidence={"tier": s.tier, "win_rate": s.primary_wr},
            ))

    worst = [r for r in per_symbol if r["exits"] >= 3 and r["pnl_usd"] < 0][:3]
    for r in worst:
        recs.append(Recommendation(
            severity="warn", category="symbol",
            message=(
                f"{r['symbol']} ({r['tier']}) dragging tier WR: "
                f"{r['wins']}W/{r['losses']}L · ${r['pnl_usd']:.2f} · "
                f"WR {(r['win_rate'] or 0)*100:.0f}%."
            ),
            evidence=r,
        ))

    total_exits = sum(s.primary_sample for s in tier_stats)
    total_wins = sum(s.n_wins for s in tier_stats)
    overall_wr = (total_wins / total_exits) if total_exits else None
    if overall_wr is not None and overall_wr < WR_RESTORE_MIN:
        recs.append(Recommendation(
            severity="warn", category="tier",
            message=(
                f"Overall canonical WR {overall_wr*100:.1f}% below restore "
                f"target {WR_RESTORE_MIN*100:.0f}%. All tiers with "
                f"sufficient sample will soft-halt until recovery."
            ),
            evidence={"overall_win_rate": overall_wr,
                      "total_exits": total_exits},
        ))
    elif overall_wr is not None:
        recs.append(Recommendation(
            severity="info", category="tier",
            message=f"Overall canonical WR {overall_wr*100:.1f}% at/above restore target.",
            evidence={"overall_win_rate": overall_wr},
        ))

    with_data = sum(1 for s in tier_stats if s.primary_sample >= MIN_SAMPLE_FOR_HALT)
    if with_data < 2:
        recs.append(Recommendation(
            severity="info", category="data",
            message=(
                f"Only {with_data}/4 tiers have >= {MIN_SAMPLE_FOR_HALT} "
                f"closed exits in {PRIMARY_WINDOW}. Halt only fires on "
                f"sufficient sample."
            ),
        ))

    return recs, experiments


def _research_notes(tier_stats: list[TierStats], now_s: float) -> list[str]:
    notes: list[str] = []
    for s in tier_stats:
        if s.n_exits >= 5 and s.n_losses > 2 * s.n_wins:
            notes.append(
                f"Tier {s.tier}: losses outnumber wins 2:1 "
                f"({s.n_losses}L vs {s.n_wins}W). Either entry bar too low "
                f"or TP/SL asymmetry hurting realized WR."
            )

    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    try:
        cutoff_ms = int((now_s - 24 * 3600) * 1000)
        rr = con.execute(
            f"SELECT tier, "
            f"  sum(CASE WHEN action='enter' THEN 1 ELSE 0 END), "
            f"  sum(CASE WHEN action='reject' THEN 1 ELSE 0 END) "
            f"FROM trade_log "
            f"WHERE ts_ms > ? AND tier IN ('A+','A','B','C') "
            f"AND {_spot_module_where_clause()} GROUP BY tier",
            (cutoff_ms,),
        ).fetchall()
    finally:
        con.close()
    for tier, enters, rejects in rr:
        if enters and rejects and rejects >= 3 * enters:
            notes.append(
                f"Tier {tier}: {rejects} rejects per {enters} enters "
                f"({rejects/enters:.1f}×). Exchange is refusing most "
                f"orders — free-USDT, min notional, or price-limit issue."
            )

    con = persist._connect()
    try:
        recon_pnl = con.execute(
            f"SELECT COALESCE(sum(pnl_usd),0) FROM trade_log "
            f"WHERE action='exit' AND module LIKE 'M_reconciled%'"
        ).fetchone()[0]
    finally:
        con.close()
    if recon_pnl and float(recon_pnl) < -1.0:
        notes.append(
            f"Reconciled cleanup has booked ${float(recon_pnl):.2f} loss "
            f"lifetime. Not canonical strategy."
        )

    if not notes:
        notes.append(
            "No single failure pattern dominates — WR softness appears "
            "broad-based. Likely candidates: composite threshold, TP/SL "
            "multiplier misalignment, regime drift."
        )
    return notes


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def run_research(
    window_h: int = 24,
    *,
    clock: Optional[Clock] = None,
    status: str = "interim",
) -> ResearchReport:
    """Execute one research pass.

    Args:
        window_h: legacy window for the top-level stats (default 24h).
        clock: optional clock() -> seconds-since-epoch. Defaults to the
            module's default clock (which is wall-clock in production and
            a frozen/advanceable test clock in CI).
        status: "interim" for the ~1h-after-halt quick report; "final"
            for the ~6h-after-halt full plan. Persisted as-is.
    """
    c = clock or _DEFAULT_CLOCK
    now_s = c()
    ts_ms = int(now_s * 1000)
    report_id = f"sr-{ts_ms}-{uuid.uuid4().hex[:6]}"
    snapshot_id = f"snap-{ts_ms}"
    audit_rollup_id = f"rollup-{ts_ms}"

    # Fetch all three windows.
    per_tier_windows: dict[str, dict[str, WindowStats]] = {
        t: {} for t in ("A+", "A", "B", "C")
    }
    for label, hours in WINDOWS_HOURS.items():
        ws = _fetch_window_stats(now_s, hours, c)
        for tier, s in ws.items():
            per_tier_windows[tier][label] = s

    # Build TierStats with primary window selected.
    tier_stats: list[TierStats] = []
    for tier in ("A+", "A", "B", "C"):
        primary = per_tier_windows[tier].get(PRIMARY_WINDOW)
        if primary is None:
            # Fallback: pick 24h if primary window missing.
            primary = per_tier_windows[tier].get("24h", WindowStats(window="24h"))
        ts = TierStats(
            tier=tier,
            windows=per_tier_windows[tier],
            primary_window=PRIMARY_WINDOW,
            primary_wr=primary.win_rate,
            primary_sample=primary.n_exits,
            primary_ci=primary.confidence_interval_95,
            n_enters=primary.n_enters,
            n_exits=primary.n_exits,
            n_wins=primary.n_wins,
            n_losses=primary.n_losses,
            total_pnl_usd=primary.total_pnl_usd,
            win_rate=primary.win_rate,
            avg_pnl_usd=(primary.total_pnl_usd / primary.n_exits) if primary.n_exits else None,
        )
        _decide_halt_verdict(ts)
        tier_stats.append(ts)

    per_symbol = _fetch_per_symbol(now_s, float(window_h))

    toggles_after, cancels = _enforce_halt(tier_stats)
    # Phase 11n-2: halt_state reflects "is this tier currently halted?"
    # which is EITHER the research verdict says halt right now OR the
    # execution toggle is off (because a prior halt transitioned it).
    # Under default (enforce OFF), `toggles_after` will always be all
    # False, so halt_state degrades to just the verdict — which is what
    # the advisory-only flow needs. Under enforce ON, toggle off sticks
    # across subsequent runs even after the rolling window empties.
    halt_state = {
        s.tier: bool(
            (s.halt_verdict == "halt") or toggles_after.get(s.tier, False)
        )
        for s in tier_stats
    }

    total_exits = sum(s.primary_sample for s in tier_stats)
    total_wins = sum(s.n_wins for s in tier_stats)
    total_pnl = sum(s.total_pnl_usd for s in tier_stats)
    overall_wr = (total_wins / total_exits) if total_exits > 0 else None

    recs, experiments = _build_recommendations(tier_stats, per_symbol)

    return ResearchReport(
        report_id=report_id,
        snapshot_id=snapshot_id,
        audit_rollup_id=audit_rollup_id,
        generated_ts_ms=ts_ms,
        status=status,
        window_h=window_h,
        tier_stats=tier_stats,
        overall_wr=overall_wr,
        overall_exits=total_exits,
        overall_pnl_usd=total_pnl,
        halt_state=halt_state,
        cancel_attempts=cancels,
        recommendations=recs,
        experiments=experiments,
        research_notes=_research_notes(tier_stats, now_s),
        evidence_refs=[
            f"db://trade_log?cutoff_ms={int((now_s-24*3600)*1000)}",
            f"tier_toggles://snapshot={json.dumps(_current_toggle_snapshot())}",
        ],
        thresholds={
            "wr_halt_min": WR_HALT_MIN,
            "wr_restore_min": WR_RESTORE_MIN,
            "wr_resume_min": WR_RESTORE_MIN,  # back-compat alias for 11l
            "min_sample_for_halt": MIN_SAMPLE_FOR_HALT,
            "primary_window": PRIMARY_WINDOW,
            "staged_sizing": float(_CFG.get("staged_sizing", 0.5)),
            # Phase 11n-8: surface enforcement state so the dashboard can
            # label the research card "advisory" vs "ENFORCING".
            "enforce_halt": os.environ.get(
                "SPOT_RESEARCH_ENFORCE_HALT", "0"
            ).strip() == "1",
        },
        per_symbol=per_symbol,
    )


# ---------------------------------------------------------------------------
# Persistence.
# ---------------------------------------------------------------------------

# Base schema — CREATE TABLE with all columns, plus the time index.
# The audit_rollup_id index is created AFTER the migration so it can't
# reference a column that doesn't exist on pre-Phase-11m DBs.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_research_reports (
    report_id        TEXT PRIMARY KEY,
    snapshot_id      TEXT,
    audit_rollup_id  TEXT,
    generated_ts_ms  INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'interim',
    window_h         INTEGER NOT NULL,
    overall_wr       REAL,
    overall_exits    INTEGER NOT NULL,
    overall_pnl_usd  REAL NOT NULL,
    n_halted_tiers   INTEGER NOT NULL,
    payload_json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_research_ts
    ON spot_research_reports(generated_ts_ms DESC);
"""


def _init_schema() -> None:
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    try:
        con.executescript(_SCHEMA)
        # Defensive column adds for pre-existing (Phase 11l) databases.
        # CREATE TABLE IF NOT EXISTS is a no-op when the table already
        # exists, so new columns need explicit ALTER on migration.
        cols = [r[1] for r in con.execute(
            "PRAGMA table_info(spot_research_reports)"
        ).fetchall()]
        for col, ddl in (
            ("snapshot_id", "ALTER TABLE spot_research_reports ADD COLUMN snapshot_id TEXT"),
            ("audit_rollup_id", "ALTER TABLE spot_research_reports ADD COLUMN audit_rollup_id TEXT"),
            ("status", "ALTER TABLE spot_research_reports ADD COLUMN status TEXT NOT NULL DEFAULT 'interim'"),
        ):
            if col not in cols:
                try: con.execute(ddl)
                except Exception: pass  # noqa: BLE001,S110
        # NOW safe to create the audit_rollup_id index — column is guaranteed.
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_spot_research_rollup "
            "ON spot_research_reports(audit_rollup_id)"
        )
        con.commit()
    finally:
        con.close()


def persist_report(r: ResearchReport) -> None:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO spot_research_reports "
            "(report_id, snapshot_id, audit_rollup_id, generated_ts_ms, "
            " status, window_h, overall_wr, overall_exits, overall_pnl_usd, "
            " n_halted_tiers, payload_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.report_id, r.snapshot_id, r.audit_rollup_id,
                r.generated_ts_ms, r.status, r.window_h, r.overall_wr,
                r.overall_exits, r.overall_pnl_usd,
                sum(1 for v in r.halt_state.values() if v),
                json.dumps(r.to_dict(), default=str),
            ),
        )
        con.commit()
    finally:
        con.close()


def latest_report() -> dict[str, Any] | None:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        row = con.execute(
            "SELECT payload_json FROM spot_research_reports "
            "ORDER BY generated_ts_ms DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    if not row: return None
    try:
        return json.loads(row[0])
    except Exception:  # noqa: BLE001
        return None


def history(
    limit: int = 24,
    *,
    start_ts_ms: Optional[int] = None,
    end_ts_ms: Optional[int] = None,
    tier: Optional[str] = None,
) -> list[dict[str, Any]]:
    """History summary. Optional filters:
      start_ts_ms / end_ts_ms — inclusive range on generated_ts_ms.
      tier — string-match against the payload (linear filter post-DB).
    """
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        clauses = ["1=1"]
        params: list[Any] = []
        if start_ts_ms is not None:
            clauses.append("generated_ts_ms >= ?"); params.append(int(start_ts_ms))
        if end_ts_ms is not None:
            clauses.append("generated_ts_ms <= ?"); params.append(int(end_ts_ms))
        where = " AND ".join(clauses)
        query = (
            "SELECT report_id, snapshot_id, audit_rollup_id, generated_ts_ms, "
            "status, window_h, overall_wr, overall_exits, overall_pnl_usd, "
            "n_halted_tiers, payload_json "
            f"FROM spot_research_reports WHERE {where} "
            "ORDER BY generated_ts_ms DESC LIMIT ?"
        )
        params.append(int(limit) * (3 if tier else 1))
        rows = con.execute(query, params).fetchall()
    finally:
        con.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        summary = {
            "report_id": r[0], "snapshot_id": r[1], "audit_rollup_id": r[2],
            "generated_ts_ms": r[3], "status": r[4], "window_h": r[5],
            "overall_wr": r[6], "overall_exits": r[7],
            "overall_pnl_usd": r[8], "n_halted_tiers": r[9],
        }
        if tier:
            try:
                payload = json.loads(r[10])
                hs = payload.get("halt_state") or {}
                # Include if tier appears in halt_state OR recommendations.
                match = tier in hs
                if not match:
                    for rec in payload.get("recommendations", []):
                        ev = rec.get("evidence") or {}
                        if ev.get("tier") == tier:
                            match = True; break
                if match:
                    out.append(summary)
            except Exception:  # noqa: BLE001
                continue
        else:
            out.append(summary)
        if len(out) >= limit:
            break
    return out


def run_and_persist(
    window_h: int = 24,
    *,
    clock: Optional[Clock] = None,
    status: str = "interim",
) -> ResearchReport:
    r = run_research(window_h=window_h, clock=clock, status=status)
    try:
        persist_report(r)
    except Exception:  # noqa: BLE001
        pass

    # Phase 11n Layer 4: validate the persisted report against the truth
    # governor. Verdict is stored in its own table; halt decisions stand
    # fail-safe even if the governor declares the report suspect.
    try:
        from spot_aggro.governance import research_truth_gov as _rtg
        _rtg.validate_and_persist(r.to_dict())
    except Exception:  # noqa: BLE001
        pass

    # Phase 11n-2 Layer 6: rebuild the Decision bundle + validate it.
    # The Decision card needs fresh factor evidence every research pass;
    # the Decision Truth Governor catches drift between factor claims
    # and action choices.
    try:
        from spot_aggro.governance import decision_engine as _de
        from spot_aggro.governance import decision_truth_gov as _dtg
        bundle = _de.build_and_persist()
        _dtg.validate_and_persist(bundle.to_dict())
    except Exception:  # noqa: BLE001
        pass

    # Phase 11n: after each research pass, run the Auto Scenario Lab on
    # every halted tier. Non-stop mode iterates permutations until it
    # finds a config with projected WR >= 60%, producing a hypothesis
    # the operator can act on. Capped small on interim (cheap), uncapped
    # on final.
    try:
        from spot_aggro.governance import scenario_runner as _sr
        cap = 36 if status == "interim" else 216
        non_stop = (status == "final")
        for tier, halted in (r.halt_state or {}).items():
            if not halted:
                continue
            _sr.run_batch_and_persist(
                tier=tier, non_stop=non_stop, cap=cap,
                clock=clock,
            )
    except Exception:  # noqa: BLE001
        pass

    return r


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def _parse_iso(s: str) -> float:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        prog="python -m openclaw_v1.spot_aggro.governance.research_agent",
        description="SPOT AGGRO win-rate research agent.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run_once", help="Run one research pass + persist")
    run_p.add_argument("--simulate-time", type=str, default=None,
                       help="ISO-8601 timestamp to freeze the clock (deterministic mode)")
    run_p.add_argument("--window-hours", type=int, default=24)
    run_p.add_argument("--status", choices=["interim", "final"], default="interim")

    lat_p = sub.add_parser("latest", help="Print the most recent report as JSON")

    hist_p = sub.add_parser("history", help="Print history summary as JSON")
    hist_p.add_argument("--start", type=str, default=None, help="ISO start")
    hist_p.add_argument("--end", type=str, default=None, help="ISO end")
    hist_p.add_argument("--tier", type=str, default=None)
    hist_p.add_argument("--limit", type=int, default=24)

    args = parser.parse_args(argv)

    if args.cmd == "run_once":
        clk: Optional[Clock] = None
        if args.simulate_time:
            frozen = _parse_iso(args.simulate_time)
            clk = lambda: frozen  # noqa: E731
        r = run_and_persist(
            window_h=args.window_hours, clock=clk, status=args.status,
        )
        print(json.dumps(r.to_dict(), indent=2, default=str))
        return 0
    if args.cmd == "latest":
        print(json.dumps(latest_report(), indent=2, default=str))
        return 0
    if args.cmd == "history":
        start_ms = int(_parse_iso(args.start) * 1000) if args.start else None
        end_ms = int(_parse_iso(args.end) * 1000) if args.end else None
        print(json.dumps(
            history(limit=args.limit, start_ts_ms=start_ms,
                    end_ts_ms=end_ms, tier=args.tier),
            indent=2, default=str,
        ))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
