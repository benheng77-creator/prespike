"""Phase 11n-9-dd — Equity-mark heartbeat writer.

Auto-heals the `equity_marks` staleness that made the dashboard show
STALE/FAIL on Trading Engine and Account Snapshot cards whenever the
operator kept the engine stopped.

Contract
--------
Every 60 seconds, writes one equity_marks row carrying the CURRENT
known equity. Source priority:

  1. `_engine_instance.status()['capital_usd']`  (if running)
  2. Latest equity_marks row (carry-forward)
  3. Configured baseline from `ops_config.yml`  (cold-start fallback)

`peak_usd` is latest(peak_usd) from prior rows (never decreases).
`positions_open` is 0 when engine is not running.

This daemon runs independently of the engine loop so halting the engine
does not stop the heartbeat. Card freshness windows see a fresh row
every minute, so Trading Engine + Account Snapshot cards stay OK
regardless of engine posture.

Fail-open: any DB or config error is logged and the loop continues;
losing one heartbeat tick is not critical, losing the writer would be.

SPOT AGGRO only. No apex_omega imports. No writes outside equity_marks.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

# 60s between heartbeats. SLA on Trading Engine / Account Snapshot cards
# is 600s (10 min), so even if a heartbeat is dropped the card stays OK.
HEARTBEAT_INTERVAL_S = 60

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_last_tick_ts_ms: Optional[int] = None


def last_tick_ts_ms() -> Optional[int]:
    """Monotonic view of when the heartbeat last ran. Used by tests and
    by /gov/heartbeat status endpoint."""
    return _last_tick_ts_ms


def _current_capital_usd() -> float:
    """Best-effort current equity for the heartbeat row."""
    try:
        from spot_aggro import _engine_instance
        if _engine_instance is not None:
            st = _engine_instance.status() or {}
            cap = st.get("capital_usd")
            if isinstance(cap, (int, float)) and cap > 0:
                return float(cap)
    except Exception:
        pass
    # Carry-forward from the latest row.
    try:
        from spot_aggro.ops.persistence.state import _connect, init_schema
        init_schema()
        con = _connect()
        try:
            r = con.execute(
                "SELECT equity_usd FROM equity_marks "
                "ORDER BY ts_ms DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()
        if r and r["equity_usd"] is not None:
            return float(r["equity_usd"])
    except Exception as e:
        log.debug("heartbeat carry-forward read failed: %s", e)
    # Cold-start baseline.
    try:
        from shared.config.ops_config import load_ops_config
        cfg = load_ops_config()
        baseline = cfg.get("initial_capital_usd")
        if isinstance(baseline, (int, float)) and baseline > 0:
            return float(baseline)
    except Exception:
        pass
    # Last-ditch: return 0.0 — better than crashing the writer.
    return 0.0


def _current_peak_usd(equity_usd: float) -> float:
    try:
        from spot_aggro.ops.persistence.state import latest_peak
        p = latest_peak()
        if p is not None and p > equity_usd:
            return float(p)
    except Exception as e:
        log.debug("heartbeat peak read failed: %s", e)
    return float(equity_usd)


def _current_positions() -> int:
    try:
        from spot_aggro import _engine_instance
        if _engine_instance is None:
            return 0
        st = _engine_instance.status() or {}
        pos = st.get("positions") or {}
        if isinstance(pos, dict):
            return len(pos)
    except Exception:
        pass
    return 0


def _write_heartbeat() -> None:
    """Write one heartbeat row. Errors are logged, never raised."""
    global _last_tick_ts_ms
    try:
        from spot_aggro.ops.persistence.state import record_equity
        eq = _current_capital_usd()
        peak = _current_peak_usd(eq)
        npos = _current_positions()
        record_equity(eq, peak, npos)
        _last_tick_ts_ms = int(time.time() * 1000)
    except Exception as e:
        log.warning("heartbeat write failed: %s", e)


def _loop() -> None:
    log.info("spot_aggro heartbeat writer started (interval=%ds)",
             HEARTBEAT_INTERVAL_S)
    # Write one immediately so cards heal on server boot.
    _write_heartbeat()
    while not _stop.is_set():
        if _stop.wait(timeout=HEARTBEAT_INTERVAL_S):
            break
        _write_heartbeat()
    log.info("spot_aggro heartbeat writer stopped")


def start() -> None:
    """Idempotent start. Safe to call from server startup."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(
        target=_loop, name="spot-aggro-heartbeat", daemon=True
    )
    _thread.start()


def stop() -> None:
    _stop.set()
