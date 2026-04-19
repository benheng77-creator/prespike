"""
Spot-local loader that turns raw `trade_log` rows into the closed-trade
dicts the WRI analyzer consumes.

Read-only. Never writes. Never touches forensic_v2/.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from shared.persistence import state as persist

log = logging.getLogger("spot_aggro.governance.trade_source")


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def load_closed_trades(
    window_start_ms: int,
    window_end_ms: int,
) -> list[dict[str, Any]]:
    """Return a list of closed-trade dicts in the [start, end) window.

    Schema matches the WRI analyzer input contract:
        symbol, tier, regime, composite_score, notional_usd,
        entry_ts_ms, exit_ts_ms, pnl_usd, pnl_pct,
        round_trip_cost_bp, expected_move_bp, exit_reason, trade_id.

    Rows that cannot be paired (no preceding enter, corrupt payload) are
    dropped — the WRI handles gaps via its confidence label.
    """
    persist.init_schema()
    con = persist._connect()
    try:
        exits = con.execute(
            """
            SELECT id, ts_ms, symbol, module, notional_usd, avg_px,
                   pnl_usd, payload_json
            FROM trade_log
            WHERE ts_ms >= ? AND ts_ms < ? AND action='exit'
            ORDER BY ts_ms ASC
            """,
            (int(window_start_ms), int(window_end_ms)),
        ).fetchall()

        out: list[dict[str, Any]] = []
        for ex in exits:
            enter = con.execute(
                """
                SELECT id, ts_ms, avg_px, payload_json
                FROM trade_log
                WHERE symbol=? AND action='enter' AND ts_ms < ?
                ORDER BY ts_ms DESC LIMIT 1
                """,
                (ex["symbol"], ex["ts_ms"]),
            ).fetchone()
            if enter is None:
                continue
            try:
                en_payload = json.loads(enter["payload_json"]) if enter["payload_json"] else {}
                ex_payload = json.loads(ex["payload_json"]) if ex["payload_json"] else {}
            except (TypeError, ValueError):
                continue

            entry_px = _safe_float(enter["avg_px"])
            exit_px = _safe_float(ex["avg_px"])
            if not entry_px or not exit_px:
                continue
            pnl_pct = (exit_px - entry_px) / entry_px if entry_px else 0.0

            out.append({
                "trade_id": f"T{ex['id']}",
                "symbol": ex["symbol"],
                "tier": en_payload.get("tier") or ex_payload.get("tier") or "?",
                "regime": (
                    en_payload.get("entry_regime")
                    or en_payload.get("regime")
                    or "UNKNOWN"
                ),
                "composite_score": _safe_float(
                    en_payload.get("composite") or en_payload.get("composite_score")
                ),
                "notional_usd": _safe_float(ex["notional_usd"]),
                "entry_ts_ms": int(enter["ts_ms"]),
                "exit_ts_ms": int(ex["ts_ms"]),
                "pnl_usd": _safe_float(ex["pnl_usd"]),
                "pnl_pct": pnl_pct,
                "round_trip_cost_bp": _safe_float(
                    en_payload.get("round_trip_cost_bp")
                    or ex_payload.get("round_trip_cost_bp")
                ),
                "expected_move_bp": _safe_float(
                    en_payload.get("expected_move_bp")
                    or ex_payload.get("expected_move_bp")
                ),
                "exit_reason": ex_payload.get("reason"),
            })
        return out
    finally:
        con.close()
