"""
spot_aggro telemetry package — SPOT AGGRO ONLY.

Three-tool integration:
    QuestDB — time-series firehose via ILP line protocol
    Grafana — reader on QuestDB via PG wire (:8812)
    Sentry  — runtime/error truth layer (dormant when DSN unset)

Contract:
    - Every emit_* function is non-blocking. If QuestDB is down, events
      queue in memory up to a bounded capacity; excess events are dropped
      oldest-first and counted. The trading loop never stalls on I/O.
    - Sentry bridge is a no-op stub until SPOT_SENTRY_BACKEND_DSN is set.
    - Apex isolation: every table name is prefixed spot_, every Sentry
      event carries tag engine=spot_aggro. Zero imports from apex_omega.
    - forensic_v2 is never imported or modified by this package.
"""
from __future__ import annotations

from .emitter import (
    init_telemetry,
    shutdown_telemetry,
    get_sink_stats,
    emit_trade,
    emit_funnel,
    emit_audit,
    emit_swarm_cycle,
    emit_consensus,
    emit_regime,
    emit_execution_cost,
    emit_coin_memory,
    emit_forensic_run,
    emit_kill,
    emit_wri,
    emit_governor,
    emit_dashboard_truth_issue,
)

__all__ = [
    "init_telemetry", "shutdown_telemetry", "get_sink_stats",
    "emit_trade", "emit_funnel", "emit_audit", "emit_swarm_cycle",
    "emit_consensus", "emit_regime", "emit_execution_cost",
    "emit_coin_memory", "emit_forensic_run", "emit_kill",
    "emit_wri", "emit_governor", "emit_dashboard_truth_issue",
]
