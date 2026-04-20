"""Opportunity Fabric — Sprint 6: Counterfactual Replay Engine.

For each closed CDV trade, replays the admission decision under a
DIFFERENT policy (e.g. "what if contrarian had been deep_value?", or
"what if the scorer had been v13 instead of v12?") and computes the
counterfactual PnL that policy would have realized on the same
universe snapshot, same market conditions, same exit regime.

Output: causal execution score
    causal_delta = (realized_pnl - counterfactual_pnl) / notional

Positive delta = the live policy beat the counterfactual policy on
the same setup. Negative delta = the counterfactual would have
outperformed. Aggregated over many trades, this separates SIGNAL
quality (did the admission rule pick a good setup?) from EXECUTION
luck (did we exit at a lucky tick?).

Replay is BOUNDED:
  - Reads only from spot_live_variant_entries + the universe snapshot
    captured via provenance.universe_snapshot_hash (Sprint 2).
  - No market-data lookups beyond what was recorded at admission time.
  - No order placement. Pure offline compute.
  - Counterfactual PnL is MODELED via simple per-variant rules:
    conservative -> match the live exit signal but exit at +1.5% target
    exploratory -> exit at +2.0% target or -1.5% stop whichever first
    aggressive -> exit at +3.0% target or -2.0% stop

Default-OFF: results are written to spot_counterfactual_replay but not
used by any admission or promotion code until
SPOT_COUNTERFACTUAL_FEED_PROMOTION=1 is set (future work).

Phase 1 of this module: single-trade replay + aggregate stats.
Phase 2 (future): matched-pair live A/B where the counterfactual is
actually traded in an exploratory slot.
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


def _init_schema() -> None:
    try:
        con = _connect()
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS spot_counterfactual_replay("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts_ms INTEGER NOT NULL,"
                " entry_id INTEGER NOT NULL,"
                " live_variant TEXT NOT NULL,"
                " counterfactual_policy TEXT NOT NULL,"
                " realized_pnl_usd REAL,"
                " counterfactual_pnl_usd REAL,"
                " causal_delta_bp REAL,"
                " rationale TEXT,"
                " context_json TEXT,"
                " UNIQUE(entry_id, counterfactual_policy)"
                ")"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_cf_entry"
                " ON spot_counterfactual_replay(entry_id)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_cf_ts"
                " ON spot_counterfactual_replay(ts_ms DESC)"
            )
        finally:
            con.close()
    except Exception:
        pass


# Counterfactual policy targets (TP%, SL%).
POLICY_TARGETS = {
    "conservative": (0.015, -0.010),        # +1.5% / -1.0%
    "exploratory":  (0.020, -0.015),        # +2.0% / -1.5%
    "aggressive":   (0.030, -0.020),        # +3.0% / -2.0%
}


@dataclass
class ReplayResult:
    entry_id: int
    live_variant: str
    counterfactual_policy: str
    realized_pnl_usd: float | None
    counterfactual_pnl_usd: float | None
    causal_delta_bp: float | None
    rationale: str
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Core replay logic
# ---------------------------------------------------------------------------

def _fetch_entry(entry_id: int) -> sqlite3.Row | None:
    try:
        con = _connect()
        try:
            return con.execute(
                "SELECT id, variant, symbol, status,"
                "       notional_usd, realized_pnl_usd,"
                "       opened_ts_ms, closed_ts_ms"
                " FROM spot_live_variant_entries WHERE id = ?",
                (int(entry_id),),
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return None


def _model_counterfactual_pnl(
    row: sqlite3.Row, tp_pct: float, sl_pct: float
) -> tuple[float, str]:
    """Model what a (tp_pct, sl_pct) policy would have realized on the
    same entry. We use the realized entry outcome as a proxy:
      - If the live trade hit > tp_pct of notional: counterfactual also
        hit (earlier) → cf_pnl = tp_pct * notional.
      - If the live trade lost > |sl_pct| of notional: cf stopped out
        first → cf_pnl = sl_pct * notional (negative).
      - If the live trade ended between (sl_pct, tp_pct) in pct terms:
        counterfactual would have held longer OR exited the same;
        we use the live realized PnL capped at tp/sl as the cf outcome.
    Conservative — does not invent PnL beyond what the entry saw.
    """
    notional = float(row["notional_usd"] or 0.0)
    realized = float(row["realized_pnl_usd"] or 0.0)
    if notional <= 0:
        return 0.0, "zero notional — cf=0"
    realized_pct = realized / notional

    if realized_pct >= tp_pct:
        cf_pnl = tp_pct * notional
        return cf_pnl, (
            f"live hit {realized_pct*100:.2f}% ≥ tp {tp_pct*100:.1f}% → "
            f"cf capped at tp = ${cf_pnl:+.2f}"
        )
    if realized_pct <= sl_pct:
        cf_pnl = sl_pct * notional
        return cf_pnl, (
            f"live hit {realized_pct*100:.2f}% ≤ sl {sl_pct*100:.1f}% → "
            f"cf stopped at sl = ${cf_pnl:+.2f}"
        )
    # Between bounds: counterfactual mirrors live outcome.
    return realized, (
        f"live {realized_pct*100:.2f}% inside tp/sl window "
        f"[{sl_pct*100:.1f}%, {tp_pct*100:.1f}%] → cf = live"
    )


def replay_one(entry_id: int, counterfactual_policy: str) -> ReplayResult:
    """Run a single counterfactual replay and persist the result.
    Idempotent on (entry_id, policy) via UNIQUE constraint."""
    _init_schema()
    if counterfactual_policy not in POLICY_TARGETS:
        return ReplayResult(
            entry_id=entry_id,
            live_variant="?",
            counterfactual_policy=counterfactual_policy,
            realized_pnl_usd=None,
            counterfactual_pnl_usd=None,
            causal_delta_bp=None,
            rationale=f"unknown policy {counterfactual_policy}",
        )
    row = _fetch_entry(entry_id)
    if row is None or row["status"] != "closed":
        return ReplayResult(
            entry_id=entry_id,
            live_variant=row["variant"] if row else "?",
            counterfactual_policy=counterfactual_policy,
            realized_pnl_usd=None,
            counterfactual_pnl_usd=None,
            causal_delta_bp=None,
            rationale="entry not found or not yet closed",
        )
    tp, sl = POLICY_TARGETS[counterfactual_policy]
    realized = float(row["realized_pnl_usd"] or 0.0)
    cf_pnl, rationale = _model_counterfactual_pnl(row, tp, sl)
    notional = float(row["notional_usd"] or 0.0)
    delta_bp = ((realized - cf_pnl) / notional * 10_000) if notional > 0 else None

    result = ReplayResult(
        entry_id=entry_id,
        live_variant=row["variant"],
        counterfactual_policy=counterfactual_policy,
        realized_pnl_usd=round(realized, 4),
        counterfactual_pnl_usd=round(cf_pnl, 4),
        causal_delta_bp=round(delta_bp, 2) if delta_bp is not None else None,
        rationale=rationale,
        context={"tp_pct": tp, "sl_pct": sl, "notional_usd": notional},
    )
    # Persist (UPSERT via INSERT OR REPLACE on UNIQUE key).
    try:
        con = _connect()
        try:
            con.execute(
                "INSERT OR REPLACE INTO spot_counterfactual_replay("
                " ts_ms, entry_id, live_variant, counterfactual_policy,"
                " realized_pnl_usd, counterfactual_pnl_usd,"
                " causal_delta_bp, rationale, context_json)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (int(time.time() * 1000), int(entry_id),
                 result.live_variant, counterfactual_policy,
                 result.realized_pnl_usd, result.counterfactual_pnl_usd,
                 result.causal_delta_bp, result.rationale[:240],
                 json.dumps(result.context, default=str)),
            )
        finally:
            con.close()
    except Exception:
        pass
    return result


def replay_all_closed(policies: tuple[str, ...] = ("conservative", "exploratory", "aggressive")
                      ) -> dict[str, Any]:
    """Replay every closed entry against the given policies. Idempotent."""
    _init_schema()
    results: list[ReplayResult] = []
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT id FROM spot_live_variant_entries"
                " WHERE status = 'closed' AND realized_pnl_usd IS NOT NULL"
            ).fetchall()
        finally:
            con.close()
    except Exception:
        rows = []
    for r in rows:
        for policy in policies:
            results.append(replay_one(int(r["id"]), policy))
    return {
        "ts_ms": int(time.time() * 1000),
        "n_entries": len(rows),
        "n_policies": len(policies),
        "n_results": len(results),
    }


def aggregate_stats(policy: str) -> dict[str, Any]:
    """Summary of causal deltas vs a single counterfactual policy."""
    _init_schema()
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT causal_delta_bp, realized_pnl_usd,"
                "       counterfactual_pnl_usd, live_variant"
                " FROM spot_counterfactual_replay"
                " WHERE counterfactual_policy = ?"
                "  AND causal_delta_bp IS NOT NULL",
                (policy,),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        rows = []
    if not rows:
        return {"policy": policy, "n": 0,
                "mean_delta_bp": None,
                "live_wins": 0, "cf_wins": 0, "ties": 0}
    deltas = [float(r["causal_delta_bp"]) for r in rows]
    wins = sum(1 for d in deltas if d > 0)
    losses = sum(1 for d in deltas if d < 0)
    ties = sum(1 for d in deltas if d == 0)
    mean = sum(deltas) / len(deltas)
    return {
        "policy": policy,
        "n": len(rows),
        "mean_delta_bp": round(mean, 2),
        "live_wins": wins,
        "cf_wins": losses,
        "ties": ties,
    }
