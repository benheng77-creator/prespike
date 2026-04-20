"""Phase 11n-9-uu Option 1 — Crypto.com data-quality audit.

Reads the `spot_exchange_comparison` rows (persisted every 60s by
phase-qq daemon) and computes rolling reliability metrics for each
(symbol, exchange) pair. Outputs feed governance verdicts and the
CDV panel so operator has evidence-grade visibility into whether
Crypto.com is worth integrating for execution.

Metrics per (symbol, exchange) over a rolling window:

  - uptime_pct         fraction of rows where ok=1
  - stale_quote_ratio  fraction of rows where bid/ask hasn't moved
                       more than 0.1bp vs prior row for >= 5 minutes
  - depth_usd_median   median top_depth_usd
  - depth_usd_p10      10th pct depth (worst-case fill scenario)
  - spread_bp_median   median spread
  - n_samples          raw row count in window

Cross-exchange (per symbol):
  - drift_p50, drift_p90, drift_p99  distribution of |okx_last - cdc_last|
  - sustained_drift_events           count of >= 30bp drifts lasting >= 5 min
  - tradability_score                0..1 composite

All metrics are pure functions over the existing table — no new
writes, no new daemon. Called on-demand by the endpoint.
"""
from __future__ import annotations

import json
import os
import sqlite3
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any


def _db_path() -> str:
    return (
        os.environ.get("TRADE_DB_PATH")
        or os.environ.get("CLAW_DB_PATH")
        or "trades.db"
    )


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path(), isolation_level=None, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


# Tunables.
STALE_BP_THRESHOLD = 0.1         # ticker movement < 0.1bp = stale
STALE_DURATION_S = 5 * 60        # stale if still < threshold after 5 min
SUSTAINED_DRIFT_BP = 30.0        # drift magnitude for actionable mispricing
SUSTAINED_DRIFT_DURATION_S = 5 * 60
DEFAULT_WINDOW_MIN = 24 * 60     # 24h default window

VALID_EXCHANGES = ("okx", "cryptocom")


@dataclass
class ExchangeSymbolQuality:
    symbol: str
    exchange: str
    n_samples: int
    uptime_pct: float
    stale_quote_ratio: float
    depth_usd_median: float
    depth_usd_p10: float
    spread_bp_median: float
    last_sample_age_s: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SymbolCrossExchange:
    symbol: str
    drift_p50_bp: float
    drift_p90_bp: float
    drift_p99_bp: float
    sustained_drift_events: int
    tradability_score: float      # 0..1 — higher = more integratable
    depth_ratio_cdc_vs_okx: float
    verdict: str                  # 'integratable'|'watch'|'unfit'

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DataQualityReport:
    window_min: int
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    per_exchange_symbol: list[ExchangeSymbolQuality] = field(default_factory=list)
    cross_exchange: list[SymbolCrossExchange] = field(default_factory=list)
    overall_integratable_count: int = 0
    overall_watch_count: int = 0
    overall_unfit_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["per_exchange_symbol"] = [
            p.to_dict() if hasattr(p, "to_dict") else dict(p)
            for p in self.per_exchange_symbol
        ]
        d["cross_exchange"] = [
            c.to_dict() if hasattr(c, "to_dict") else dict(c)
            for c in self.cross_exchange
        ]
        return d


def _percentile(vs: list[float], pct: float) -> float:
    if not vs:
        return 0.0
    xs = sorted(vs)
    k = max(0, min(len(xs) - 1, int(len(xs) * pct)))
    return xs[k]


def _compute_stale_ratio(rows: list[sqlite3.Row]) -> float:
    """Fraction of samples whose last movement (bid+ask delta) was below
    STALE_BP_THRESHOLD for at least STALE_DURATION_S seconds."""
    if len(rows) < 2:
        return 0.0
    # rows ordered by ts_ms asc.
    stale = 0
    for i, r in enumerate(rows):
        # Find the earliest prior sample within STALE_DURATION_S.
        cutoff_ts = r["ts_ms"] - STALE_DURATION_S * 1000
        anchor = None
        for j in range(i - 1, -1, -1):
            if rows[j]["ts_ms"] < cutoff_ts:
                break
            anchor = rows[j]
        if not anchor:
            continue
        last_now = float(r["last"] or 0)
        last_anchor = float(anchor["last"] or 0)
        if last_anchor <= 0 or last_now <= 0:
            continue
        bp = abs(last_now - last_anchor) / last_anchor * 10_000.0
        if bp < STALE_BP_THRESHOLD:
            stale += 1
    return round(stale / len(rows), 4)


def _quality_one(symbol: str, exchange: str, window_min: int
                 ) -> ExchangeSymbolQuality:
    cutoff_ms = int(time.time() * 1000) - window_min * 60_000
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT ts_ms, last, bid, ask, spread_bp,"
                " bid_depth_usd, ask_depth_usd, top_depth_usd, ok"
                " FROM spot_exchange_comparison"
                " WHERE symbol = ? AND exchange = ? AND ts_ms >= ?"
                " ORDER BY ts_ms ASC",
                (symbol, exchange, cutoff_ms),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        rows = []
    n = len(rows)
    if n == 0:
        return ExchangeSymbolQuality(
            symbol=symbol, exchange=exchange,
            n_samples=0, uptime_pct=0.0, stale_quote_ratio=0.0,
            depth_usd_median=0.0, depth_usd_p10=0.0,
            spread_bp_median=0.0, last_sample_age_s=None,
        )
    n_ok = sum(1 for r in rows if int(r["ok"] or 0) == 1)
    depths = [float(r["top_depth_usd"] or 0) for r in rows if r["top_depth_usd"] is not None]
    spreads = [float(r["spread_bp"] or 0) for r in rows if r["spread_bp"] is not None]
    last_ts = int(rows[-1]["ts_ms"])
    age_s = (int(time.time() * 1000) - last_ts) // 1000
    return ExchangeSymbolQuality(
        symbol=symbol, exchange=exchange,
        n_samples=n,
        uptime_pct=round(n_ok / n, 4),
        stale_quote_ratio=_compute_stale_ratio(rows),
        depth_usd_median=(
            round(statistics.median(depths), 2) if depths else 0.0
        ),
        depth_usd_p10=round(_percentile(depths, 0.10), 2) if depths else 0.0,
        spread_bp_median=(
            round(statistics.median(spreads), 2) if spreads else 0.0
        ),
        last_sample_age_s=age_s,
    )


def _compute_cross_exchange(symbol: str, window_min: int
                            ) -> SymbolCrossExchange:
    cutoff_ms = int(time.time() * 1000) - window_min * 60_000
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT ts_ms, exchange, last, top_depth_usd"
                " FROM spot_exchange_comparison"
                " WHERE symbol = ? AND ts_ms >= ?"
                " ORDER BY ts_ms ASC",
                (symbol, cutoff_ms),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        rows = []

    # Group by ts_ms window (within 60s of each other) for paired compare.
    paired: list[tuple[int, float, float, float, float]] = []
    # dict[ts_bucket][exchange] = (last, depth)
    by_bucket: dict[int, dict[str, tuple[float, float]]] = {}
    for r in rows:
        bucket = int(r["ts_ms"]) // 60_000
        by_bucket.setdefault(bucket, {})[r["exchange"]] = (
            float(r["last"] or 0), float(r["top_depth_usd"] or 0),
        )
    for bucket, exs in by_bucket.items():
        okx = exs.get("okx")
        cdc = exs.get("cryptocom")
        if not (okx and cdc):
            continue
        if okx[0] <= 0 or cdc[0] <= 0:
            continue
        mid = (okx[0] + cdc[0]) / 2.0
        drift_bp = abs(okx[0] - cdc[0]) / mid * 10_000.0
        paired.append((bucket, okx[0], cdc[0], okx[1], cdc[1]))
        if drift_bp:
            pass  # placeholder; computed below

    drifts: list[float] = []
    for b, okx_l, cdc_l, _, _ in paired:
        mid = (okx_l + cdc_l) / 2.0
        drifts.append(abs(okx_l - cdc_l) / mid * 10_000.0 if mid else 0.0)

    # Sustained-drift detection: sequences of >=5 consecutive buckets
    # where every drift >= SUSTAINED_DRIFT_BP. Bucket width = 60s so
    # 5 consecutive = 5min.
    sustained = 0
    i = 0
    while i < len(drifts):
        if drifts[i] >= SUSTAINED_DRIFT_BP:
            run = 0
            while i < len(drifts) and drifts[i] >= SUSTAINED_DRIFT_BP:
                run += 1
                i += 1
            if run >= 5:
                sustained += 1
        else:
            i += 1

    # Depth ratio (CDC / OKX) from latest bucket.
    depth_ratio = 0.0
    if paired:
        _, _, _, okx_d, cdc_d = paired[-1]
        depth_ratio = (cdc_d / okx_d) if okx_d > 0 else 0.0

    # Tradability score: composite.
    # - drift moderate (5-30bp is OK; too tight = no arb, too loose = chaos)
    # - n_samples sufficient
    # - sustained events >= 1 indicate real inefficiency
    n = len(drifts)
    p50 = _percentile(drifts, 0.5) if drifts else 0.0
    p90 = _percentile(drifts, 0.9) if drifts else 0.0
    p99 = _percentile(drifts, 0.99) if drifts else 0.0
    # Score components.
    drift_component = min(max(p90 / 30.0, 0.0), 1.0)          # p90 up to 30bp
    sustained_component = min(sustained / 3.0, 1.0)           # 3+ events saturate
    sample_component = min(n / 60.0, 1.0)                     # 60+ = full confidence
    # Penalty for extreme instability (p99 > 200bp = weird book).
    stability_penalty = 1.0 - min(max((p99 - 200) / 200.0, 0.0), 1.0)
    score = round(
        (drift_component * 0.3 + sustained_component * 0.4
         + sample_component * 0.2 + stability_penalty * 0.1),
        4,
    )

    if score >= 0.55 and sustained >= 1:
        verdict = "integratable"
    elif score >= 0.30:
        verdict = "watch"
    else:
        verdict = "unfit"

    return SymbolCrossExchange(
        symbol=symbol,
        drift_p50_bp=round(p50, 2),
        drift_p90_bp=round(p90, 2),
        drift_p99_bp=round(p99, 2),
        sustained_drift_events=sustained,
        tradability_score=score,
        depth_ratio_cdc_vs_okx=round(depth_ratio, 2),
        verdict=verdict,
    )


def _tracked_symbols() -> list[str]:
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT DISTINCT symbol FROM spot_exchange_comparison"
            ).fetchall()
        finally:
            con.close()
        return sorted([r["symbol"] for r in rows])
    except Exception:
        return []


def generate_report(window_min: int = DEFAULT_WINDOW_MIN) -> DataQualityReport:
    """Public entrypoint — computes full audit report across all
    tracked symbols and both exchanges."""
    rpt = DataQualityReport(window_min=window_min)
    symbols = _tracked_symbols()
    # Per (symbol, exchange) quality.
    for s in symbols:
        for ex in VALID_EXCHANGES:
            rpt.per_exchange_symbol.append(_quality_one(s, ex, window_min))
    # Cross-exchange for each symbol.
    for s in symbols:
        ce = _compute_cross_exchange(s, window_min)
        rpt.cross_exchange.append(ce)
        if ce.verdict == "integratable":
            rpt.overall_integratable_count += 1
        elif ce.verdict == "watch":
            rpt.overall_watch_count += 1
        else:
            rpt.overall_unfit_count += 1
    return rpt
