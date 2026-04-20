"""Phase 11n-9-uu Option 2 — CDC integration-trigger governance rule.

Records per-event signals when Crypto.com would likely have delivered
a better fill than OKX. Accumulates these into a verdict:

  none        — no signal evidence yet
  watching    — some signals observed; below actionable threshold
  actionable  — >= ACTIONABLE_THRESHOLD signals in 24h; Phase 2
                integration evidence bar met

Signal sources:

  1. admit_blocked_thin_okx — an OKX-only depth read blocked a variant
     that would have admitted against CDC's depth
  2. fill_slippage_gap     — a real OKX fill had slippage > X bp when
     CDC's mid was within tolerance
  3. sustained_drift_okx_pay — price drift >= 30bp where the direction
     would have helped the in-flight trade

Never writes to live trading paths. Pure observer.
"""
from __future__ import annotations

import json
import os
import sqlite3
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


ACTIONABLE_THRESHOLD = 20         # >= 20 signals in 24h => actionable
WATCHING_THRESHOLD = 5            # >= 5 signals in 24h => watching

# Signal kinds.
KIND_ADMIT_BLOCKED = "admit_blocked_thin_okx"
KIND_FILL_SLIP_GAP = "fill_slippage_gap"
KIND_DRIFT_PAYOFF = "sustained_drift_okx_pay"


def _init_schema() -> None:
    con = _connect()
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS spot_integration_triggers("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts_ms INTEGER NOT NULL,"
            " kind TEXT NOT NULL,"
            " symbol TEXT NOT NULL,"
            " variant TEXT,"
            " okx_value REAL,"
            " cdc_value REAL,"
            " gap_bp REAL,"
            " notes TEXT,"
            " payload_json TEXT"
            ")"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_itrig_ts"
            " ON spot_integration_triggers(ts_ms DESC)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_itrig_kind_ts"
            " ON spot_integration_triggers(kind, ts_ms DESC)"
        )
    finally:
        con.close()


@dataclass
class IntegrationSignal:
    ts_ms: int
    kind: str
    symbol: str
    variant: str | None
    okx_value: float | None
    cdc_value: float | None
    gap_bp: float | None
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def record_signal(
    kind: str, symbol: str,
    variant: str | None = None,
    okx_value: float | None = None,
    cdc_value: float | None = None,
    gap_bp: float | None = None,
    notes: str = "",
    extra: dict[str, Any] | None = None,
) -> int:
    """Fail-open signal writer. Returns row id, 0 on failure."""
    try:
        _init_schema()
        now = int(time.time() * 1000)
        con = _connect()
        try:
            cur = con.execute(
                "INSERT INTO spot_integration_triggers("
                " ts_ms, kind, symbol, variant, okx_value, cdc_value,"
                " gap_bp, notes, payload_json"
                ") VALUES(?,?,?,?,?,?,?,?,?)",
                (now, kind, symbol, variant, okx_value, cdc_value,
                 gap_bp, notes[:200], json.dumps(extra or {})),
            )
            return int(cur.lastrowid or 0)
        finally:
            con.close()
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Detection helpers — called opportunistically by governance daemon
# ---------------------------------------------------------------------------

def scan_admit_blocked_signals(window_min: int = 60) -> int:
    """Scan shadow authz + exchange comparison tables jointly. When a
    CDV variant rejected a coin for `liquid` reason AND CDC had >10x
    OKX depth at that time, record an admit_blocked signal.

    Returns number of NEW signals written."""
    _init_schema()
    cutoff_ms = int(time.time() * 1000) - window_min * 60_000
    try:
        con = _connect()
        try:
            # Rows where a CDV variant rejected with 'liquid' in reason.
            rejected = con.execute(
                "SELECT ts_ms, symbol, variant, reason"
                " FROM shadow_variant_authorizations"
                " WHERE variant IN ('contrarian','deep_value')"
                " AND variant_passed = 0"
                " AND ts_ms >= ?"
                " AND reason LIKE '%liquid%'"
                " ORDER BY ts_ms DESC LIMIT 200",
                (cutoff_ms,),
            ).fetchall()
            # Already-signaled keys to avoid duplicate writes.
            existing = con.execute(
                "SELECT ts_ms, symbol FROM spot_integration_triggers"
                " WHERE kind = ? AND ts_ms >= ?",
                (KIND_ADMIT_BLOCKED, cutoff_ms),
            ).fetchall()
            seen = {(r["ts_ms"] // 60_000, r["symbol"]) for r in existing}
        finally:
            con.close()
    except Exception:
        return 0

    n_written = 0
    for r in rejected:
        bucket = int(r["ts_ms"]) // 60_000
        key = (bucket, r["symbol"])
        if key in seen:
            continue
        # Query the closest exchange_comparison rows for this (symbol, ts).
        try:
            con = _connect()
            try:
                # OKX + CDC rows in the 2-minute window around the authz.
                low = int(r["ts_ms"]) - 120_000
                high = int(r["ts_ms"]) + 120_000
                xr = con.execute(
                    "SELECT exchange, top_depth_usd, last FROM spot_exchange_comparison"
                    " WHERE symbol = ? AND ts_ms BETWEEN ? AND ? AND ok = 1"
                    " ORDER BY ts_ms ASC",
                    (r["symbol"], low, high),
                ).fetchall()
            finally:
                con.close()
        except Exception:
            continue

        okx_depth = 0.0
        cdc_depth = 0.0
        for x in xr:
            if x["exchange"] == "okx":
                okx_depth = max(okx_depth, float(x["top_depth_usd"] or 0))
            elif x["exchange"] == "cryptocom":
                cdc_depth = max(cdc_depth, float(x["top_depth_usd"] or 0))
        if okx_depth > 0 and cdc_depth >= 10 * okx_depth:
            record_signal(
                kind=KIND_ADMIT_BLOCKED,
                symbol=r["symbol"],
                variant=r["variant"],
                okx_value=okx_depth,
                cdc_value=cdc_depth,
                gap_bp=0.0,
                notes=(
                    f"CDC depth {cdc_depth / max(okx_depth, 1):.1f}x OKX; "
                    f"reason={r['reason'][:80]}"
                ),
            )
            n_written += 1
            seen.add(key)
    return n_written


def scan_sustained_drift_signals(window_min: int = 60) -> int:
    """Detect buckets where |OKX-CDC| >= 30bp for >= 5 consecutive
    minutes. These are actionable mispricing windows."""
    _init_schema()
    cutoff_ms = int(time.time() * 1000) - window_min * 60_000
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT ts_ms, symbol, exchange, last"
                " FROM spot_exchange_comparison"
                " WHERE ts_ms >= ? AND ok = 1"
                " ORDER BY symbol ASC, ts_ms ASC",
                (cutoff_ms,),
            ).fetchall()
            existing = con.execute(
                "SELECT ts_ms, symbol FROM spot_integration_triggers"
                " WHERE kind = ? AND ts_ms >= ?",
                (KIND_DRIFT_PAYOFF, cutoff_ms),
            ).fetchall()
            seen = {(r["ts_ms"] // 300_000, r["symbol"]) for r in existing}
        finally:
            con.close()
    except Exception:
        return 0

    # Bucket per minute per symbol.
    by_sym: dict[str, dict[int, dict[str, float]]] = {}
    for r in rows:
        bucket = int(r["ts_ms"]) // 60_000
        by_sym.setdefault(r["symbol"], {}).setdefault(bucket, {})[r["exchange"]] = float(r["last"] or 0)

    n_written = 0
    for sym, buckets in by_sym.items():
        sorted_b = sorted(buckets.keys())
        run_len = 0
        run_start_ms = 0
        max_drift_bp = 0.0
        last_okx = 0.0
        last_cdc = 0.0
        for b in sorted_b:
            ex = buckets[b]
            okx_l = ex.get("okx", 0.0)
            cdc_l = ex.get("cryptocom", 0.0)
            if okx_l <= 0 or cdc_l <= 0:
                run_len = 0
                continue
            mid = (okx_l + cdc_l) / 2.0
            drift_bp = abs(okx_l - cdc_l) / mid * 10_000.0 if mid else 0.0
            if drift_bp >= 30.0:
                if run_len == 0:
                    run_start_ms = b * 60_000
                run_len += 1
                max_drift_bp = max(max_drift_bp, drift_bp)
                last_okx = okx_l
                last_cdc = cdc_l
            else:
                if run_len >= 5:
                    ev_bucket = run_start_ms // 300_000
                    if (ev_bucket, sym) not in seen:
                        record_signal(
                            kind=KIND_DRIFT_PAYOFF,
                            symbol=sym,
                            variant=None,
                            okx_value=last_okx,
                            cdc_value=last_cdc,
                            gap_bp=max_drift_bp,
                            notes=f"sustained {run_len}min drift max={max_drift_bp:.1f}bp",
                        )
                        n_written += 1
                        seen.add((ev_bucket, sym))
                run_len = 0
                max_drift_bp = 0.0
        # Trailing run (still active at window end).
        if run_len >= 5:
            ev_bucket = run_start_ms // 300_000
            if (ev_bucket, sym) not in seen:
                record_signal(
                    kind=KIND_DRIFT_PAYOFF,
                    symbol=sym,
                    variant=None,
                    okx_value=last_okx,
                    cdc_value=last_cdc,
                    gap_bp=max_drift_bp,
                    notes=f"sustained {run_len}min drift (active) max={max_drift_bp:.1f}bp",
                )
                n_written += 1
                seen.add((ev_bucket, sym))
    return n_written


# ---------------------------------------------------------------------------
# Verdict computation
# ---------------------------------------------------------------------------

@dataclass
class TriggerVerdict:
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    n_signals_24h: int = 0
    n_by_kind: dict[str, int] = field(default_factory=dict)
    cdc_value_signal: str = "none"       # 'none' | 'watching' | 'actionable'
    top_symbols: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_verdict(window_min: int = 24 * 60) -> TriggerVerdict:
    """Roll up signals into a verdict. Also runs the two scan helpers
    so state stays fresh per call."""
    _init_schema()
    # Refresh scans opportunistically.
    try:
        scan_admit_blocked_signals(window_min=min(window_min, 60))
    except Exception:
        pass
    try:
        scan_sustained_drift_signals(window_min=min(window_min, 60))
    except Exception:
        pass

    cutoff_ms = int(time.time() * 1000) - window_min * 60_000
    v = TriggerVerdict()
    try:
        con = _connect()
        try:
            total = con.execute(
                "SELECT COUNT(*) AS n FROM spot_integration_triggers"
                " WHERE ts_ms >= ?",
                (cutoff_ms,),
            ).fetchone()
            by_kind = con.execute(
                "SELECT kind, COUNT(*) AS n FROM spot_integration_triggers"
                " WHERE ts_ms >= ? GROUP BY kind",
                (cutoff_ms,),
            ).fetchall()
            top = con.execute(
                "SELECT symbol, COUNT(*) AS n, MAX(gap_bp) AS max_gap"
                " FROM spot_integration_triggers"
                " WHERE ts_ms >= ? GROUP BY symbol"
                " ORDER BY n DESC LIMIT 5",
                (cutoff_ms,),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return v

    v.n_signals_24h = int(total["n"] or 0) if total else 0
    v.n_by_kind = {r["kind"]: int(r["n"] or 0) for r in by_kind}
    v.top_symbols = [
        {"symbol": r["symbol"], "count": int(r["n"] or 0),
         "max_gap_bp": float(r["max_gap"] or 0)}
        for r in top
    ]
    if v.n_signals_24h >= ACTIONABLE_THRESHOLD:
        v.cdc_value_signal = "actionable"
        v.reason = (
            f"{v.n_signals_24h} integration signals in {window_min}min "
            f">= {ACTIONABLE_THRESHOLD} threshold. CDC integration "
            f"evidence bar met. Review top_symbols + by_kind before Phase 2."
        )
    elif v.n_signals_24h >= WATCHING_THRESHOLD:
        v.cdc_value_signal = "watching"
        v.reason = (
            f"{v.n_signals_24h} integration signals in {window_min}min. "
            f"Accumulating evidence; {ACTIONABLE_THRESHOLD} total needed "
            f"for actionable verdict."
        )
    else:
        v.cdc_value_signal = "none"
        v.reason = (
            f"Only {v.n_signals_24h} signals in {window_min}min. "
            f"Crypto.com integration not yet justified by live data."
        )
    return v


def latest_signals(limit: int = 20) -> list[dict[str, Any]]:
    _init_schema()
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT * FROM spot_integration_triggers"
                " ORDER BY ts_ms DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        finally:
            con.close()
        return [dict(r) for r in rows]
    except Exception:
        return []
