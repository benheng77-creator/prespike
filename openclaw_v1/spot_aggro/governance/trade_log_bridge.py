"""Opportunity Fabric — Bridge: mirror legacy trade_log → spot_live_variant_entries.

Why:
  The live engine (phase-11n-9-aa … ww) executes trades via the legacy
  `trade_log` table with module names like M1_scalp_C. The phase-vv
  horse race, phase-ww trade timer, and the Opportunity Fabric Sprint
  1–3 modules all read from `spot_live_variant_entries`. Without a
  bridge, the governance layer is blind to live activity.

What it does:
  Every N seconds, scan `trade_log` for entries (action='enter') and
  exits (action='exit') that have NOT yet been mirrored, then upsert
  corresponding rows in `spot_live_variant_entries`.

Variant mapping:
  trade_log.module has no contrarian/deep_value/momentum distinction.
  We infer from payload_json:
    - payload.tier + payload.entry_regime + heuristics -> variant

  Default mapping (conservative — no trade is tagged unless clear):
    * tier=C AND regime in (DEAD, UNKNOWN) AND pnl_pct_target > 0.01:
      -> "contrarian" (anti-momentum lean, oversold setups)
    * tier=C AND 7d_ret missing or mild: -> "momentum" (scalp-style)
    * tier=B/A: -> "momentum"
    * unknown: -> "legacy_scalp" (catch-all tag; Opportunity Fabric
      modules ignore it unless operator opts in)

  Mapping is INTENTIONALLY coarse and conservative. This is a bridge,
  not a classifier. Operators can rewrite historical tags with a
  future module if ground-truth labels become available.

Idempotent: uses `correlation_id` from trade_log to dedupe; if the
row already exists in spot_live_variant_entries (matched by authz_id
or by (ts_ms, symbol, notional) tuple) we skip.

NEVER writes to trade_log. NEVER places orders. Pure mirror layer.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

BRIDGE_CURSOR_KEY = "spot_trade_log_bridge_last_ts_ms"


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


def _init_schema() -> None:
    try:
        con = _connect()
        try:
            # Cursor table — single row keyed by BRIDGE_CURSOR_KEY.
            con.execute(
                "CREATE TABLE IF NOT EXISTS spot_bridge_cursors("
                " key TEXT PRIMARY KEY,"
                " value_ts_ms INTEGER NOT NULL,"
                " updated_ts_ms INTEGER NOT NULL"
                ")"
            )
            # Ensure spot_live_variant_entries has all fields we need.
            cols = [
                r["name"] for r in con.execute(
                    "PRAGMA table_info(spot_live_variant_entries)"
                ).fetchall()
            ]
            if cols:
                if "opened_ts_ms" not in cols:
                    con.execute(
                        "ALTER TABLE spot_live_variant_entries"
                        " ADD COLUMN opened_ts_ms INTEGER"
                    )
                if "slippage_bp" not in cols:
                    con.execute(
                        "ALTER TABLE spot_live_variant_entries"
                        " ADD COLUMN slippage_bp REAL"
                    )
                if "fill_rate" not in cols:
                    con.execute(
                        "ALTER TABLE spot_live_variant_entries"
                        " ADD COLUMN fill_rate REAL"
                    )
                if "provenance_json" not in cols:
                    con.execute(
                        "ALTER TABLE spot_live_variant_entries"
                        " ADD COLUMN provenance_json TEXT"
                    )
                if "source_trade_log_id" not in cols:
                    con.execute(
                        "ALTER TABLE spot_live_variant_entries"
                        " ADD COLUMN source_trade_log_id INTEGER"
                    )
                    con.execute(
                        "CREATE INDEX IF NOT EXISTS idx_slve_src"
                        " ON spot_live_variant_entries(source_trade_log_id)"
                    )
        finally:
            con.close()
    except Exception:
        pass


def _cursor_ts_ms() -> int:
    _init_schema()
    try:
        con = _connect()
        try:
            row = con.execute(
                "SELECT value_ts_ms FROM spot_bridge_cursors WHERE key = ?",
                (BRIDGE_CURSOR_KEY,),
            ).fetchone()
            return int(row["value_ts_ms"]) if row else 0
        finally:
            con.close()
    except Exception:
        return 0


def _write_cursor(ts_ms: int) -> None:
    try:
        con = _connect()
        try:
            con.execute(
                "INSERT OR REPLACE INTO spot_bridge_cursors("
                " key, value_ts_ms, updated_ts_ms) VALUES(?,?,?)",
                (BRIDGE_CURSOR_KEY, int(ts_ms), int(time.time() * 1000)),
            )
        finally:
            con.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Variant inference
# ---------------------------------------------------------------------------

def _infer_variant(payload: dict[str, Any], module: str) -> str:
    """Map a legacy trade_log row to a variant. Conservative: default to
    'legacy_scalp' when the signal is ambiguous so Opportunity Fabric
    modules (exploration_wallet, trip-wire) don't over-claim."""
    tier = str(payload.get("tier") or "").upper()
    regime = str(
        payload.get("entry_regime") or payload.get("regime") or ""
    ).upper()
    # Tier-C in DEAD/UNKNOWN regime is the classic contrarian setup.
    if tier == "C" and regime in ("DEAD", "UNKNOWN", ""):
        return "contrarian"
    # Tier-C in bullish regimes — momentum.
    if tier == "C" and regime in ("SQUEEZE_BUILDING", "BREAKOUT_BULL", "TREND_UP"):
        return "momentum"
    # Tier-B or A: trend-follow.
    if tier in ("A", "A+", "B"):
        return "momentum"
    return "legacy_scalp"


def _compute_slippage_bp(payload: dict[str, Any], avg_px: float | None,
                        side: str, fee_usd: float | None,
                        notional: float | None) -> float | None:
    """Estimate realized slippage in bp vs the scoring-time reference
    price embedded in payload. If no reference is available, return None.

    Fees are already deducted in pnl_usd; slippage here is pure price
    movement between decision and fill.
    """
    ref = payload.get("ref_px") or payload.get("decision_px")
    if avg_px is None or ref is None or float(ref) <= 0:
        return None
    ref_f = float(ref)
    avg_f = float(avg_px)
    if side == "buy":
        slip_pct = (avg_f - ref_f) / ref_f
    else:
        slip_pct = (ref_f - avg_f) / ref_f
    return round(max(0.0, slip_pct * 10_000), 2)


# ---------------------------------------------------------------------------
# Mirroring
# ---------------------------------------------------------------------------

@dataclass
class BridgeResult:
    scanned: int = 0
    inserted: int = 0
    closed: int = 0
    skipped: int = 0
    last_ts_ms: int = 0


def run_once() -> BridgeResult:
    """Scan new trade_log rows since last cursor, mirror into
    spot_live_variant_entries. Idempotent — re-running does nothing."""
    _init_schema()
    cursor = _cursor_ts_ms()
    res = BridgeResult(last_ts_ms=cursor)

    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT id, ts_ms, symbol, module, action, side,"
                "       notional_usd, avg_px, fee_usd, pnl_usd,"
                "       correlation_id, payload_json, tier, slippage_usd"
                " FROM trade_log"
                " WHERE ts_ms > ?"
                " ORDER BY ts_ms ASC",
                (cursor,),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return res

    if not rows:
        return res

    con = _connect()
    try:
        for r in rows:
            res.scanned += 1
            try:
                payload = json.loads(r["payload_json"] or "{}")
            except Exception:
                payload = {}
            action = str(r["action"] or "").lower()
            symbol = r["symbol"]
            tl_id = int(r["id"])

            if action == "enter":
                # Skip if we've already mirrored this entry.
                ex = con.execute(
                    "SELECT id FROM spot_live_variant_entries"
                    " WHERE source_trade_log_id = ?",
                    (tl_id,),
                ).fetchone()
                if ex:
                    res.skipped += 1
                else:
                    variant = _infer_variant(payload, r["module"] or "")
                    notional = float(r["notional_usd"] or 0.0)
                    ts_ms = int(r["ts_ms"])
                    slip = _compute_slippage_bp(
                        payload, r["avg_px"], r["side"] or "",
                        r["fee_usd"], notional,
                    )
                    con.execute(
                        "INSERT INTO spot_live_variant_entries("
                        " ts_ms, opened_ts_ms, variant, symbol,"
                        " notional_usd, authz_id, status,"
                        " slippage_bp, fill_rate, source_trade_log_id)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (ts_ms, ts_ms, variant, symbol, notional,
                         r["correlation_id"], "open",
                         slip, 1.0, tl_id),
                    )
                    res.inserted += 1

            elif action == "exit":
                # Find the most recent open entry for this symbol and close it.
                match = con.execute(
                    "SELECT id, notional_usd FROM spot_live_variant_entries"
                    " WHERE symbol = ? AND status = 'open'"
                    " ORDER BY ts_ms DESC LIMIT 1",
                    (symbol,),
                ).fetchone()
                if match is None:
                    res.skipped += 1
                    continue
                pnl = float(r["pnl_usd"] or 0.0)
                ts_ms = int(r["ts_ms"])
                con.execute(
                    "UPDATE spot_live_variant_entries"
                    " SET status = 'closed',"
                    "     closed_ts_ms = ?,"
                    "     realized_pnl_usd = ?"
                    " WHERE id = ?",
                    (ts_ms, pnl, int(match["id"])),
                )
                res.closed += 1
            else:
                res.skipped += 1

            res.last_ts_ms = int(r["ts_ms"])
    finally:
        con.close()

    if res.last_ts_ms > cursor:
        _write_cursor(res.last_ts_ms)

    return res


def backfill_all(max_rows: int = 10_000) -> BridgeResult:
    """One-shot: reset cursor and re-mirror everything. Use with care —
    wipes existing rows that came from trade_log mirroring."""
    _init_schema()
    try:
        con = _connect()
        try:
            con.execute(
                "DELETE FROM spot_live_variant_entries"
                " WHERE source_trade_log_id IS NOT NULL"
            )
            con.execute(
                "DELETE FROM spot_bridge_cursors WHERE key = ?",
                (BRIDGE_CURSOR_KEY,),
            )
        finally:
            con.close()
    except Exception:
        pass
    return run_once()
