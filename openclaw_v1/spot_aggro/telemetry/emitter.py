"""
Public emit_* API. Thin adapters, never block, never raise.

Safe to call from any module including the engine heartbeat.
If telemetry is disabled or infra is down, every emit is a cheap no-op
that still counts drops.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from . import metrics
from . import sentry_bridge
from .config import TelemetryConfig, load_config
from .questdb_sink import QuestDBSink

log = logging.getLogger("spot_aggro.telemetry.emitter")

_cfg: Optional[TelemetryConfig] = None
_sink: Optional[QuestDBSink] = None


def init_telemetry(cfg: Optional[TelemetryConfig] = None) -> None:
    """Idempotent. Called by server.py startup hook."""
    global _cfg, _sink
    _cfg = cfg or load_config()
    if not _cfg.enabled:
        log.info("SPOT_TELEMETRY_ENABLED=0 — telemetry disabled")
        return
    if _sink is None:
        _sink = QuestDBSink(
            host=_cfg.questdb_host,
            port=_cfg.questdb_ilp_port,
            capacity=_cfg.queue_capacity,
            connect_timeout_s=_cfg.connect_timeout_s,
            send_timeout_s=_cfg.send_timeout_s,
        )
        _sink.start()
    sentry_bridge.init_sentry(_cfg)


def shutdown_telemetry() -> None:
    global _sink
    if _sink is not None:
        try:
            _sink.stop(_cfg.worker_stop_timeout_s if _cfg else 2.0)
        except Exception as exc:  # noqa: BLE001
            log.warning("sink stop error: %s", exc)
    _sink = None


def get_sink_stats() -> dict:
    if _sink is None:
        return {"sent": 0, "dropped": 0, "reconnects": 0, "errors": 0,
                "queued": 0, "qsize": 0, "capacity": 0, "connected": False,
                "enabled": False}
    s = _sink.get_stats()
    s["enabled"] = True
    return s


def _now_ns() -> int:
    return time.time_ns()


def _safe_emit(table: str, tags: dict, fields: dict) -> None:
    """Emit → sink. Never raises. No-op when sink not running."""
    if _sink is None:
        return
    try:
        _sink.emit(table, tags, fields, _now_ns())
    except Exception as exc:  # noqa: BLE001
        log.warning("sink emit failed (%s): %s", table, exc)


# ============================================================================
# Public emit_* functions
# ============================================================================

def emit_trade(
    action: str, *, symbol: str, tier: Optional[str] = None,
    module: Optional[str] = None, notional_usd: Optional[float] = None,
    avg_px: Optional[float] = None, fee_usd: Optional[float] = None,
    pnl_usd: Optional[float] = None, reason: Optional[str] = None,
    composite: Optional[float] = None, spi: Optional[float] = None,
    correlation_id: Optional[str] = None,
) -> None:
    tags, fields = metrics.trade_row(
        action, symbol=symbol, tier=tier, module=module,
        notional_usd=notional_usd, avg_px=avg_px, fee_usd=fee_usd,
        pnl_usd=pnl_usd, reason=reason, composite=composite, spi=spi,
        correlation_id=correlation_id,
    )
    _safe_emit("spot_trades", tags, fields)


def emit_funnel(
    *, window_min: int, scored: int, tier_passed: int,
    consensus_fired: int, consensus_passed: int,
    orders_entered: int, orders_rejected: int, orders_exited: int,
) -> None:
    tags, fields = metrics.funnel_row(
        window_min=window_min, scored=scored, tier_passed=tier_passed,
        consensus_fired=consensus_fired, consensus_passed=consensus_passed,
        orders_entered=orders_entered, orders_rejected=orders_rejected,
        orders_exited=orders_exited,
    )
    _safe_emit("spot_funnel", tags, fields)


def emit_audit(
    *, symbol: str, verdict: str, reason_code: Optional[str],
    quorum_size: int, posterior_final: Optional[float],
    total_cost_usd: Optional[float], total_latency_ms: Optional[int],
    adjudicator_invoked: bool,
) -> None:
    tags, fields = metrics.audit_row(
        symbol=symbol, verdict=verdict, reason_code=reason_code,
        quorum_size=quorum_size, required_quorum=4,
        posterior_final=posterior_final, total_cost_usd=total_cost_usd,
        total_latency_ms=total_latency_ms,
        adjudicator_invoked=adjudicator_invoked,
    )
    _safe_emit("spot_audit_swarm", tags, fields)
    # Hard REJECT reasons → Sentry event (dormant if DSN unset).
    if reason_code in ("AS-001", "AS-006", "AS-008"):
        sentry_bridge.capture_message(
            f"audit_swarm {verdict} {reason_code} {symbol}",
            level="warning",
            tags={"reason_code": reason_code, "symbol": symbol,
                  "component": "audit_swarm"},
        )


def emit_swarm_cycle(
    *, layer: str, duration_ms: int, agents_ok: int, agents_total: int,
    coins_cycled: int = 0, cost_usd: Optional[float] = None,
    consecutive_errors: int = 0, last_error: Optional[str] = None,
) -> None:
    tags, fields = metrics.swarm_cycle_row(
        layer=layer, duration_ms=duration_ms, agents_ok=agents_ok,
        agents_total=agents_total, coins_cycled=coins_cycled,
        cost_usd=cost_usd, consecutive_errors=consecutive_errors,
        last_error=last_error,
    )
    _safe_emit("spot_swarm_cycles", tags, fields)
    if consecutive_errors and consecutive_errors >= 3:
        sentry_bridge.capture_message(
            f"swarm {layer} consecutive_errors={consecutive_errors}",
            level="error",
            tags={"layer": layer, "component": "swarm_runner"},
        )


def emit_consensus(
    *, symbol: str, consensus: float, conflict: float, vetoed: bool,
    members_called: int, members_ok: int,
) -> None:
    tags, fields = metrics.consensus_row(
        symbol=symbol, consensus=consensus, conflict=conflict,
        vetoed=vetoed, members_called=members_called, members_ok=members_ok,
    )
    _safe_emit("spot_consensus", tags, fields)


def emit_regime(
    *, regime: str, regime_confidence: Optional[float] = None,
    squeeze_timing: Optional[str] = None, edge_status: Optional[str] = None,
    universe_quality: Optional[str] = None,
) -> None:
    tags, fields = metrics.regime_row(
        regime=regime, regime_confidence=regime_confidence,
        squeeze_timing=squeeze_timing, edge_status=edge_status,
        universe_quality=universe_quality,
    )
    _safe_emit("spot_regime", tags, fields)


def emit_execution_cost(
    *, symbol: str, notional_usd: Optional[float] = None,
    ref_price: Optional[float] = None, fill_price: Optional[float] = None,
    slippage_bp: Optional[float] = None, half_spread_bp: Optional[float] = None,
    fee_bp: Optional[float] = None, maker_or_taker: Optional[str] = None,
    round_trip_cost_bp: Optional[float] = None,
    expected_move_bp: Optional[float] = None,
) -> None:
    tags, fields = metrics.execution_cost_row(
        symbol=symbol, notional_usd=notional_usd, ref_price=ref_price,
        fill_price=fill_price, slippage_bp=slippage_bp,
        half_spread_bp=half_spread_bp, fee_bp=fee_bp,
        maker_or_taker=maker_or_taker,
        round_trip_cost_bp=round_trip_cost_bp,
        expected_move_bp=expected_move_bp,
    )
    _safe_emit("spot_execution_cost", tags, fields)


def emit_coin_memory(
    *, symbol: str, tier: str, regime: str, n_trades: int, wins: int,
    win_rate: Optional[float] = None, sum_pnl: Optional[float] = None,
    composite_mult: Optional[float] = None,
    cooldown_until_ts_ns: Optional[int] = None, suppressed: bool = False,
) -> None:
    tags, fields = metrics.coin_memory_row(
        symbol=symbol, tier=tier, regime=regime, n_trades=n_trades,
        wins=wins, win_rate=win_rate, sum_pnl=sum_pnl,
        composite_mult=composite_mult,
        cooldown_until_ts_ns=cooldown_until_ts_ns, suppressed=suppressed,
    )
    _safe_emit("spot_coin_memory_log", tags, fields)


def emit_forensic_run(
    *, report_id: str, window_h: float, verdict: str, n_trades: int,
    win_rate: Optional[float] = None, exp_per_trade: Optional[float] = None,
    quorum_ok: int = 0, quorum_total: int = 0,
    cost_usd: Optional[float] = None, governor_verdict: Optional[str] = None,
    trust_score: Optional[float] = None,
) -> None:
    tags, fields = metrics.forensic_run_row(
        report_id=report_id, window_h=window_h, verdict=verdict,
        n_trades=n_trades, win_rate=win_rate,
        exp_per_trade=exp_per_trade, quorum_ok=quorum_ok,
        quorum_total=quorum_total, cost_usd=cost_usd,
        governor_verdict=governor_verdict, trust_score=trust_score,
    )
    _safe_emit("spot_forensic_runs", tags, fields)
    if (governor_verdict or "").upper() == "REJECTED":
        sentry_bridge.capture_message(
            f"forensic report REJECTED by governor — report_id={report_id}",
            level="warning",
            tags={"report_id": report_id, "component": "forensic_governor"},
        )


def emit_kill(
    *, kind: str, reason: Optional[str], dd_pct: Optional[float] = None,
    equity_usd: Optional[float] = None,
) -> None:
    tags, fields = metrics.kill_row(
        kind=kind, reason=reason, dd_pct=dd_pct, equity_usd=equity_usd,
    )
    _safe_emit("spot_kill_events", tags, fields)
    sentry_bridge.capture_message(
        f"kill_switch {kind} reason={reason or '?'}",
        level="error",
        tags={"component": "kill_switch"},
    )


def emit_wri(
    *, cadence: str, n_trades: int, win_rate: Optional[float] = None,
    total_pnl: Optional[float] = None, top_cause: Optional[str] = None,
    confidence: Optional[str] = None,
) -> None:
    tags, fields = metrics.wri_row(
        cadence=cadence, n_trades=n_trades, win_rate=win_rate,
        total_pnl=total_pnl, top_cause=top_cause, confidence=confidence,
    )
    _safe_emit("spot_wri_runs", tags, fields)


def emit_governor(
    *, kind: str, report_id: Optional[str], verdict: str,
    trust_score: Optional[float] = None, n_unverifiable: int = 0,
    n_contradictions: int = 0, n_unsupported: int = 0,
) -> None:
    tags, fields = metrics.governor_row(
        kind=kind, report_id=report_id, verdict=verdict,
        trust_score=trust_score, n_unverifiable=n_unverifiable,
        n_contradictions=n_contradictions, n_unsupported=n_unsupported,
    )
    _safe_emit("spot_governor_runs", tags, fields)


def emit_dashboard_truth_issue(
    *, kind: str, card: str, severity: str,
    summary_value: Optional[float] = None, body_value: Optional[float] = None,
    age_s: Optional[int] = None,
) -> None:
    tags, fields = metrics.dashboard_truth_issue_row(
        kind=kind, card=card, severity=severity,
        summary_value=summary_value, body_value=body_value, age_s=age_s,
    )
    _safe_emit("spot_dashboard_truth_issues", tags, fields)
