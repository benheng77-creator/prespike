"""
Win-Rate Root-Cause Investigator — pure analyzer.

Consumes a list of closed-trade dicts and produces a diagnostic report:

    {
      "window": {...},
      "totals": {n, wins, losses, win_rate, total_pnl_usd, ...},
      "win_rate_drag_table": [
          {cluster: "bad_friction_economics", lost_trades, drag_pct,
           confidence, evidence_sample: [...]}
          ...
      ],
      "red_trade_clusters": [
          {label, rule, n_trades, mean_pnl_pct, example_ids}
      ],
      "symbol_tier_regime_loss_map": [
          {symbol, tier, regime, n, win_rate, total_pnl_usd, confidence}
      ],
      "top_root_causes": [
          {cause, classification: temporary|structural|insufficient_data,
           confidence, evidence_n}
      ],
      "per_tier": { "A+": {...}, "A": {...}, "B": {...}, "C": {...} },
      "confidence": "PROVEN" | "LIKELY" | "WEAK" | "UNVERIFIABLE",
    }

This module does not talk to LLMs. It reads structured trade rows and
returns a deterministic diagnostic. LLM-enriched narrative is possible
later but orthogonal: this layer must always produce a verdict even when
LLMs are unavailable, to preserve the "always-on" contract.

It does NOT place, size, or cancel trades. It does NOT consult capital or
account equity. It does NOT override any gate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

log = logging.getLogger("spot_aggro.governance.wri")

# Confidence labels — same vocabulary as forensic_v2 for downstream parity.
CONF_PROVEN       = "PROVEN"
CONF_LIKELY       = "LIKELY"
CONF_WEAK         = "WEAK"
CONF_UNVERIFIABLE = "UNVERIFIABLE"

# Red-trade cluster labels — stable strings.
CLUSTER_BAD_ENTRY            = "bad_entry"
CLUSTER_BAD_STATE_REGIME     = "bad_state_regime"
CLUSTER_BAD_CALIBRATION      = "bad_score_calibration"
CLUSTER_BAD_FRICTION         = "bad_friction_economics"
CLUSTER_BAD_EXIT             = "bad_exit_handling"
CLUSTER_SYMBOL_TIER_WEAKNESS = "symbol_tier_regime_weakness"

ALL_CLUSTERS = (
    CLUSTER_BAD_ENTRY,
    CLUSTER_BAD_STATE_REGIME,
    CLUSTER_BAD_CALIBRATION,
    CLUSTER_BAD_FRICTION,
    CLUSTER_BAD_EXIT,
    CLUSTER_SYMBOL_TIER_WEAKNESS,
)


# ---------------------------------------------------------------------------
# Input contract
# ---------------------------------------------------------------------------
# A "trade" dict is expected to carry:
#   symbol, tier, regime, composite_score, notional_usd,
#   entry_ts_ms, exit_ts_ms, pnl_usd, pnl_pct,
#   round_trip_cost_bp, expected_move_bp,
#   exit_reason ("SL" | "TP" | "TIME_STOP" | "COMPOSITE_DECAY" | "MANUAL" | ...),
#   trade_id (str, for evidence_sample references).
#
# Missing fields degrade the cluster rule to WEAK confidence for that
# cluster.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_red(trade: dict[str, Any]) -> bool:
    pnl = trade.get("pnl_usd")
    if pnl is None:
        pnl = trade.get("pnl_pct")
    try:
        return float(pnl) < 0
    except (TypeError, ValueError):
        return False


def _is_green(trade: dict[str, Any]) -> bool:
    pnl = trade.get("pnl_usd")
    if pnl is None:
        pnl = trade.get("pnl_pct")
    try:
        return float(pnl) > 0
    except (TypeError, ValueError):
        return False


def _confidence_for_n(
    n: int, *, likely_min: int, proven_min: int,
) -> str:
    if n >= proven_min:
        return CONF_PROVEN
    if n >= likely_min:
        return CONF_LIKELY
    if n > 0:
        return CONF_WEAK
    return CONF_UNVERIFIABLE


# ---------------------------------------------------------------------------
# Cluster rules — each returns (matched_trades, reason)
# ---------------------------------------------------------------------------

def _cluster_bad_entry(trades: list[dict[str, Any]]) -> list[dict]:
    """Red trades whose composite score at entry was in the lower half
    (<=0.5) — the engine fired on a marginal signal."""
    out = []
    for t in trades:
        if not _is_red(t):
            continue
        cs = t.get("composite_score")
        if cs is None:
            continue
        try:
            if float(cs) <= 0.5:
                out.append(t)
        except (TypeError, ValueError):
            continue
    return out


def _cluster_bad_state_regime(trades: list[dict[str, Any]]) -> list[dict]:
    """Red trades whose regime is DEAD/DEAD_CHOP/UNSTABLE — should never
    have been entered."""
    bad_regimes = {"DEAD", "DEAD_CHOP", "UNSTABLE", "RANGE_BOUND"}
    out = []
    for t in trades:
        if not _is_red(t):
            continue
        r = (t.get("regime") or "").upper()
        if r in bad_regimes:
            out.append(t)
    return out


def _cluster_bad_calibration(trades: list[dict[str, Any]]) -> list[dict]:
    """Red trades whose (tier, symbol, regime, decile) bucket has produced
    a losing average across this window — calibration doesn't protect."""
    # Build per-bucket aggregate, then pick red trades whose bucket mean<0.
    agg: dict[tuple, list[float]] = {}
    for t in trades:
        key = (
            t.get("tier"), t.get("symbol"), t.get("regime"),
            _decile(t.get("composite_score")),
        )
        agg.setdefault(key, []).append(float(t.get("pnl_pct") or 0.0))
    bad_keys = {
        k for k, v in agg.items()
        if len(v) >= 3 and (sum(v) / len(v)) < 0
    }
    return [
        t for t in trades
        if _is_red(t)
        and (
            t.get("tier"), t.get("symbol"), t.get("regime"),
            _decile(t.get("composite_score")),
        ) in bad_keys
    ]


def _cluster_bad_friction(trades: list[dict[str, Any]]) -> list[dict]:
    """Red trades where expected_move_bp <= 2 * round_trip_cost_bp — edge
    never covered friction."""
    out = []
    for t in trades:
        if not _is_red(t):
            continue
        em = t.get("expected_move_bp")
        cost = t.get("round_trip_cost_bp")
        if em is None or cost is None:
            continue
        try:
            if float(em) <= 2.0 * float(cost):
                out.append(t)
        except (TypeError, ValueError):
            continue
    return out


def _cluster_bad_exit(trades: list[dict[str, Any]]) -> list[dict]:
    """Red trades whose exit was SL or TIME_STOP or COMPOSITE_DECAY — not a
    clean TP-driven exit. These suggest the exit policy never reached its
    intended profit target."""
    bad_exits = {"SL", "TIME_STOP", "COMPOSITE_DECAY", "DECAY", "STOP"}
    out = []
    for t in trades:
        if not _is_red(t):
            continue
        reason = (t.get("exit_reason") or "").upper()
        if any(b in reason for b in bad_exits):
            out.append(t)
    return out


def _cluster_symbol_tier_weakness(
    trades: list[dict[str, Any]],
    *, min_n: int,
) -> list[dict]:
    """Red trades from a (symbol, tier, regime) triple whose window-level
    win rate is < 0.35 AND n >= min_n."""
    triples: dict[tuple, list[dict]] = {}
    for t in trades:
        k = (t.get("symbol"), t.get("tier"), t.get("regime"))
        triples.setdefault(k, []).append(t)
    weak_keys: set[tuple] = set()
    for k, bucket in triples.items():
        if len(bucket) < min_n:
            continue
        wins = sum(1 for x in bucket if _is_green(x))
        wr = wins / len(bucket)
        if wr < 0.35:
            weak_keys.add(k)
    return [
        t for t in trades
        if _is_red(t)
        and (t.get("symbol"), t.get("tier"), t.get("regime")) in weak_keys
    ]


def _decile(score: Any) -> int:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return 0
    if s <= 0:
        return 0
    if s >= 1:
        return 9
    return int(s * 10)


# ---------------------------------------------------------------------------
# Classification: temporary / structural / insufficient_data
# ---------------------------------------------------------------------------

def _classify_root_cause(
    cluster_trades: list[dict[str, Any]],
    all_trades: list[dict[str, Any]],
) -> str:
    """A cause is:
      - `insufficient_data` if cluster n < 5
      - `structural` if cluster drag_pct >= 0.25 of total red trades
      - `temporary` otherwise
    Heuristic; confidence label is reported separately.
    """
    n = len(cluster_trades)
    if n < 5:
        return "insufficient_data"
    reds = sum(1 for t in all_trades if _is_red(t))
    if reds == 0:
        return "insufficient_data"
    share = n / reds
    if share >= 0.25:
        return "structural"
    return "temporary"


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WRIConfig:
    cluster_min_trades: int = 5
    drag_floor_pct: float = 0.10
    tier_min_trades_for_likely: int = 10
    tier_min_trades_for_proven: int = 30


def analyze(
    trades: Iterable[dict[str, Any]],
    *,
    window_start_ms: int,
    window_end_ms: int,
    cadence: str,
    cfg: Optional[WRIConfig] = None,
) -> dict[str, Any]:
    """Run the WRI over `trades`. Always returns a report; the report's
    `confidence` field labels trustworthiness."""
    cfg = cfg or WRIConfig()
    trades = list(trades)
    n = len(trades)

    wins = sum(1 for t in trades if _is_green(t))
    losses = sum(1 for t in trades if _is_red(t))
    total_pnl = sum(float(t.get("pnl_usd") or 0.0) for t in trades)
    win_rate: Optional[float] = (wins / n) if n > 0 else None

    # Per-cluster analysis
    cluster_fns = {
        CLUSTER_BAD_ENTRY:          lambda: _cluster_bad_entry(trades),
        CLUSTER_BAD_STATE_REGIME:   lambda: _cluster_bad_state_regime(trades),
        CLUSTER_BAD_CALIBRATION:    lambda: _cluster_bad_calibration(trades),
        CLUSTER_BAD_FRICTION:       lambda: _cluster_bad_friction(trades),
        CLUSTER_BAD_EXIT:           lambda: _cluster_bad_exit(trades),
        CLUSTER_SYMBOL_TIER_WEAKNESS: lambda: _cluster_symbol_tier_weakness(
            trades, min_n=cfg.cluster_min_trades,
        ),
    }

    clusters_detail: list[dict[str, Any]] = []
    drag_table: list[dict[str, Any]] = []
    for label, fn in cluster_fns.items():
        matches = fn()
        if not matches:
            continue
        cn = len(matches)
        drag_pct = (cn / losses) if losses > 0 else 0.0
        mean_pct = (
            sum(float(t.get("pnl_pct") or 0.0) for t in matches) / cn
            if cn else 0.0
        )
        conf = _confidence_for_n(
            cn,
            likely_min=cfg.tier_min_trades_for_likely,
            proven_min=cfg.tier_min_trades_for_proven,
        )
        clusters_detail.append({
            "label": label,
            "rule": _rule_for_label(label),
            "n_trades": cn,
            "mean_pnl_pct": round(mean_pct, 6),
            "example_ids": [t.get("trade_id") for t in matches[:5] if t.get("trade_id")],
            "confidence": conf,
        })
        if cn >= cfg.cluster_min_trades and drag_pct >= cfg.drag_floor_pct:
            drag_table.append({
                "cluster": label,
                "lost_trades": cn,
                "drag_pct": round(drag_pct, 4),
                "confidence": conf,
                "evidence_sample": [t.get("trade_id") for t in matches[:3] if t.get("trade_id")],
                "classification": _classify_root_cause(matches, trades),
            })

    # Rank drag_table by lost_trades desc; pick top 3 as root causes.
    drag_table.sort(key=lambda r: r["lost_trades"], reverse=True)
    top_root_causes = [
        {
            "cause": r["cluster"],
            "classification": r["classification"],
            "confidence": r["confidence"],
            "evidence_n": r["lost_trades"],
        }
        for r in drag_table[:3]
    ]

    # symbol × tier × regime loss map
    stg: dict[tuple, list[dict]] = {}
    for t in trades:
        k = (t.get("symbol"), t.get("tier"), t.get("regime"))
        stg.setdefault(k, []).append(t)
    loss_map: list[dict[str, Any]] = []
    for (sym, tier, regime), bucket in sorted(stg.items(), key=lambda x: -len(x[1])):
        bn = len(bucket)
        bw = sum(1 for x in bucket if _is_green(x))
        bp = sum(float(x.get("pnl_usd") or 0.0) for x in bucket)
        loss_map.append({
            "symbol": sym, "tier": tier, "regime": regime,
            "n": bn,
            "win_rate": round(bw / bn, 4) if bn else None,
            "total_pnl_usd": round(bp, 4),
            "confidence": _confidence_for_n(
                bn, likely_min=cfg.tier_min_trades_for_likely,
                proven_min=cfg.tier_min_trades_for_proven,
            ),
        })

    # Per-tier summary — all three tiers always present
    per_tier: dict[str, Any] = {}
    for tier in ("A+", "A", "B", "C"):
        bucket = [t for t in trades if t.get("tier") == tier]
        bn = len(bucket)
        bw = sum(1 for x in bucket if _is_green(x))
        bp = sum(float(x.get("pnl_usd") or 0.0) for x in bucket)
        per_tier[tier] = {
            "n": bn,
            "wins": bw,
            "losses": sum(1 for x in bucket if _is_red(x)),
            "win_rate": round(bw / bn, 4) if bn else None,
            "total_pnl_usd": round(bp, 4),
            "confidence": _confidence_for_n(
                bn, likely_min=cfg.tier_min_trades_for_likely,
                proven_min=cfg.tier_min_trades_for_proven,
            ),
        }

    overall_conf = _confidence_for_n(
        n, likely_min=cfg.tier_min_trades_for_likely,
        proven_min=cfg.tier_min_trades_for_proven,
    )

    return {
        "cadence": cadence,
        "window": {
            "start_ts_ms": window_start_ms,
            "end_ts_ms": window_end_ms,
            "duration_h": round((window_end_ms - window_start_ms) / 3_600_000.0, 3),
        },
        "totals": {
            "n_trades": n,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 4) if win_rate is not None else None,
            "total_pnl_usd": round(total_pnl, 4),
        },
        "win_rate_drag_table": drag_table,
        "red_trade_clusters": clusters_detail,
        "symbol_tier_regime_loss_map": loss_map,
        "top_root_causes": top_root_causes,
        "per_tier": per_tier,
        "confidence": overall_conf,
    }


def _rule_for_label(label: str) -> str:
    return {
        CLUSTER_BAD_ENTRY:            "red trade with composite_score <= 0.5",
        CLUSTER_BAD_STATE_REGIME:     "red trade in DEAD/DEAD_CHOP/UNSTABLE/RANGE_BOUND regime",
        CLUSTER_BAD_CALIBRATION:      "red trade in a bucket with negative window-mean pnl",
        CLUSTER_BAD_FRICTION:         "red trade with expected_move_bp <= 2 * round_trip_cost_bp",
        CLUSTER_BAD_EXIT:             "red trade exited via SL / TIME_STOP / COMPOSITE_DECAY",
        CLUSTER_SYMBOL_TIER_WEAKNESS: "red trade inside a symbol×tier×regime with WR<0.35",
    }.get(label, "")
