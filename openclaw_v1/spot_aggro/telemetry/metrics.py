"""
Row-shape helpers. The emitter calls these to produce (tags, fields)
dicts with consistent keys across every event class, which keeps the
Grafana query surface stable.
"""
from __future__ import annotations

from typing import Any, Optional


def _s(v: Any) -> str:
    """String coercion for tag columns. None/empty → empty string (sink
    filters these out)."""
    if v is None:
        return ""
    return str(v)


def trade_row(
    action: str, *, symbol: str, tier: Optional[str], module: Optional[str],
    notional_usd: Optional[float], avg_px: Optional[float],
    fee_usd: Optional[float], pnl_usd: Optional[float],
    reason: Optional[str], composite: Optional[float],
    spi: Optional[float], correlation_id: Optional[str],
) -> tuple[dict, dict]:
    tags = {
        "action": _s(action),
        "symbol": _s(symbol),
        "tier": _s(tier or "?"),
        "module": _s(module or ""),
        "reason": _s(reason or ""),
        "correlation_id": _s(correlation_id or ""),
    }
    fields = {
        "notional_usd": _f(notional_usd),
        "avg_px": _f(avg_px),
        "fee_usd": _f(fee_usd),
        "pnl_usd": _f(pnl_usd),
        "composite": _f(composite),
        "spi": _f(spi),
    }
    return tags, fields


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def funnel_row(
    *, window_min: int, scored: int, tier_passed: int,
    consensus_fired: int, consensus_passed: int,
    orders_entered: int, orders_rejected: int, orders_exited: int,
) -> tuple[dict, dict]:
    tags: dict = {}
    fields = {
        "window_min": _i(window_min),
        "scored": _i(scored),
        "tier_passed": _i(tier_passed),
        "consensus_fired": _i(consensus_fired),
        "consensus_passed": _i(consensus_passed),
        "orders_entered": _i(orders_entered),
        "orders_rejected": _i(orders_rejected),
        "orders_exited": _i(orders_exited),
    }
    return tags, fields


def audit_row(
    *, symbol: str, verdict: str, reason_code: Optional[str],
    quorum_size: int, required_quorum: int,
    posterior_final: Optional[float], total_cost_usd: Optional[float],
    total_latency_ms: Optional[int], adjudicator_invoked: bool,
) -> tuple[dict, dict]:
    tags = {
        "symbol": _s(symbol),
        "verdict": _s(verdict),
        "reason_code": _s(reason_code or ""),
    }
    fields = {
        "quorum_size": _i(quorum_size),
        "required_quorum": _i(required_quorum),
        "posterior_final": _f(posterior_final),
        "total_cost_usd": _f(total_cost_usd),
        "total_latency_ms": _i(total_latency_ms),
        "adjudicator_invoked": bool(adjudicator_invoked),
    }
    return tags, fields


def swarm_cycle_row(
    *, layer: str, duration_ms: int, agents_ok: int, agents_total: int,
    coins_cycled: int, cost_usd: Optional[float],
    consecutive_errors: int, last_error: Optional[str],
) -> tuple[dict, dict]:
    tags = {
        "layer": _s(layer),
        "last_error": _s(last_error or ""),
    }
    fields = {
        "duration_ms": _i(duration_ms),
        "agents_ok": _i(agents_ok),
        "agents_total": _i(agents_total),
        "coins_cycled": _i(coins_cycled),
        "cost_usd": _f(cost_usd),
        "consecutive_errors": _i(consecutive_errors),
    }
    return tags, fields


def consensus_row(
    *, symbol: str, consensus: float, conflict: float, vetoed: bool,
    members_called: int, members_ok: int,
) -> tuple[dict, dict]:
    tags = {"symbol": _s(symbol)}
    fields = {
        "consensus": _f(consensus),
        "conflict": _f(conflict),
        "vetoed": bool(vetoed),
        "members_called": _i(members_called),
        "members_ok": _i(members_ok),
    }
    return tags, fields


def regime_row(
    *, regime: str, regime_confidence: Optional[float],
    squeeze_timing: Optional[str], edge_status: Optional[str],
    universe_quality: Optional[str],
) -> tuple[dict, dict]:
    tags = {
        "regime": _s(regime),
        "squeeze_timing": _s(squeeze_timing or ""),
        "edge_status": _s(edge_status or ""),
        "universe_quality": _s(universe_quality or ""),
    }
    fields = {"regime_confidence": _f(regime_confidence)}
    return tags, fields


def execution_cost_row(
    *, symbol: str, notional_usd: Optional[float],
    ref_price: Optional[float], fill_price: Optional[float],
    slippage_bp: Optional[float], half_spread_bp: Optional[float],
    fee_bp: Optional[float], maker_or_taker: Optional[str],
    round_trip_cost_bp: Optional[float], expected_move_bp: Optional[float],
) -> tuple[dict, dict]:
    tags = {
        "symbol": _s(symbol),
        "maker_or_taker": _s(maker_or_taker or ""),
    }
    fields = {
        "notional_usd": _f(notional_usd),
        "ref_price": _f(ref_price),
        "fill_price": _f(fill_price),
        "slippage_bp": _f(slippage_bp),
        "half_spread_bp": _f(half_spread_bp),
        "fee_bp": _f(fee_bp),
        "round_trip_cost_bp": _f(round_trip_cost_bp),
        "expected_move_bp": _f(expected_move_bp),
    }
    return tags, fields


def coin_memory_row(
    *, symbol: str, tier: str, regime: str, n_trades: int, wins: int,
    win_rate: Optional[float], sum_pnl: Optional[float],
    composite_mult: Optional[float], cooldown_until_ts_ns: Optional[int],
    suppressed: bool,
) -> tuple[dict, dict]:
    tags = {
        "symbol": _s(symbol),
        "tier": _s(tier or "?"),
        "regime": _s(regime or ""),
    }
    fields = {
        "n_trades": _i(n_trades),
        "wins": _i(wins),
        "win_rate": _f(win_rate),
        "sum_pnl": _f(sum_pnl),
        "composite_mult": _f(composite_mult),
        "cooldown_until_ts": _i(cooldown_until_ts_ns),
        "suppressed": bool(suppressed),
    }
    return tags, fields


def forensic_run_row(
    *, report_id: str, window_h: float, verdict: str, n_trades: int,
    win_rate: Optional[float], exp_per_trade: Optional[float],
    quorum_ok: int, quorum_total: int, cost_usd: Optional[float],
    governor_verdict: Optional[str], trust_score: Optional[float],
) -> tuple[dict, dict]:
    tags = {
        "report_id": _s(report_id),
        "verdict": _s(verdict),
        "governor_verdict": _s(governor_verdict or "NONE"),
    }
    fields = {
        "window_h": _f(window_h),
        "n_trades": _i(n_trades),
        "win_rate": _f(win_rate),
        "exp_per_trade": _f(exp_per_trade),
        "quorum_ok": _i(quorum_ok),
        "quorum_total": _i(quorum_total),
        "cost_usd": _f(cost_usd),
        "trust_score": _f(trust_score),
    }
    return tags, fields


def kill_row(
    *, kind: str, reason: Optional[str], dd_pct: Optional[float],
    equity_usd: Optional[float],
) -> tuple[dict, dict]:
    tags = {"kind": _s(kind), "reason": _s(reason or "")}
    fields = {"dd_pct": _f(dd_pct), "equity_usd": _f(equity_usd)}
    return tags, fields


def wri_row(
    *, cadence: str, n_trades: int, win_rate: Optional[float],
    total_pnl: Optional[float], top_cause: Optional[str],
    confidence: Optional[str],
) -> tuple[dict, dict]:
    tags = {
        "cadence": _s(cadence),
        "top_cause": _s(top_cause or ""),
        "confidence": _s(confidence or ""),
    }
    fields = {
        "n_trades": _i(n_trades),
        "win_rate": _f(win_rate),
        "total_pnl": _f(total_pnl),
    }
    return tags, fields


def governor_row(
    *, kind: str, report_id: Optional[str], verdict: str,
    trust_score: Optional[float], n_unverifiable: int,
    n_contradictions: int, n_unsupported: int,
) -> tuple[dict, dict]:
    tags = {
        "kind": _s(kind),
        "report_id": _s(report_id or ""),
        "verdict": _s(verdict),
    }
    fields = {
        "trust_score": _f(trust_score),
        "n_unverifiable": _i(n_unverifiable),
        "n_contradictions": _i(n_contradictions),
        "n_unsupported": _i(n_unsupported),
    }
    return tags, fields


def dashboard_truth_issue_row(
    *, kind: str, card: str, severity: str,
    summary_value: Optional[float], body_value: Optional[float],
    age_s: Optional[int],
) -> tuple[dict, dict]:
    tags = {
        "kind": _s(kind),
        "card": _s(card),
        "severity": _s(severity),
    }
    fields = {
        "summary_value": _f(summary_value),
        "body_value": _f(body_value),
        "age_s": _i(age_s),
    }
    return tags, fields
