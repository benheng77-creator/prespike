"""Opportunity Fabric — Sprint 5: Passive liquidity inference.

====================================================================
REGULATORY ACKNOWLEDGMENT (required by build plan):
Active liquidity probing — placing real orders and cancelling them
before fill to measure book behavior — is indistinguishable from
spoofing under OKX's terms of service and MAS securities regulations
applicable to this account. Active probing WILL NOT be built into
this codebase.

This module implements **passive liquidity inference only**:
derived entirely from public order-book snapshots and the operator's
own trade prints. No resting orders placed for measurement purposes.
No book pings. No spoofing patterns.

If active probing is ever requested, the requester must (1) provide
written approval from OKX's institutional desk specifying probe size,
frequency, and symbols, and (2) obtain MAS legal review. Neither
condition is met as of the commit that introduces this module.
====================================================================

Inputs (all already collected by existing daemons):
  - spot_exchange_comparison: top-of-book snapshots every 60s
  - spot_live_variant_entries: the operator's own fills with
    slippage_bp / fill_rate (from Sprint 3)

Inferred signals:
  - book_imbalance       : bid_depth / (bid_depth + ask_depth)
  - thinness_score       : 1 - min(top_depth_usd / 10_000, 1)
                           0 = deep book, 1 = paper-thin
  - realized_slip_recent : operator's own mean slippage on this
                           symbol, last 24h, as lived reality
  - expected_slip_bp     : weighted combination of book-state + realized

Output: `liquidity_score` in [0, 1] where 0 = tradeable, 1 = avoid.

Default-OFF as a hard gate: advisory metric only. Variants can weight
it into their scoring; admission code can opt in via `should_block()`.
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Any


SCORE_ABORT_THRESHOLD = float(os.environ.get("SPOT_LIQ_ABORT_SCORE", "0.75"))
TOP_DEPTH_FULL_USD = float(os.environ.get("SPOT_LIQ_DEPTH_FULL_USD", "10000"))


def gate_enabled() -> bool:
    return os.environ.get("SPOT_LIQ_INFERENCE_GATE", "0").strip() == "1"


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


@dataclass
class LiquidityReading:
    symbol: str
    ts_ms: int
    # Book-state signals
    bid_depth_usd: float | None
    ask_depth_usd: float | None
    top_depth_usd: float | None
    spread_bp: float | None
    book_imbalance: float | None           # 0.5 = balanced, >0.5 = bid-heavy
    thinness_score: float | None           # 0 = deep, 1 = paper-thin
    # Own-flow signals
    n_own_fills_24h: int
    realized_slippage_bp_mean: float | None
    realized_fill_rate: float | None
    # Composite
    liquidity_score: float                 # 0 = tradeable, 1 = avoid
    expected_slippage_bp: float | None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _book_state(symbol: str) -> sqlite3.Row | None:
    try:
        con = _connect()
        try:
            # Most recent comparison snapshot on either venue.
            r = con.execute(
                "SELECT ts_ms, exchange, last, bid, ask, spread_bp,"
                "       bid_depth_usd, ask_depth_usd, top_depth_usd, ok"
                " FROM spot_exchange_comparison"
                " WHERE symbol = ? ORDER BY ts_ms DESC LIMIT 1",
                (symbol,),
            ).fetchone()
            return r
        finally:
            con.close()
    except Exception:
        return None


def _own_flow_stats(symbol: str) -> dict[str, Any]:
    """Operator's own fills on this symbol, last 24h."""
    cutoff_ms = int(time.time() * 1000) - 86_400_000
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT slippage_bp, fill_rate FROM spot_live_variant_entries"
                " WHERE symbol = ? AND closed_ts_ms >= ?"
                "  AND status = 'closed'",
                (symbol, cutoff_ms),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        rows = []
    slips = [float(r["slippage_bp"]) for r in rows
             if r["slippage_bp"] is not None]
    fills = [float(r["fill_rate"]) for r in rows
             if r["fill_rate"] is not None]
    return {
        "n": len(rows),
        "slip_mean": (sum(slips) / len(slips)) if slips else None,
        "fill_mean": (sum(fills) / len(fills)) if fills else None,
    }


def _compute_score(
    book: sqlite3.Row | None,
    own: dict[str, Any],
) -> tuple[float, float | None, str]:
    """Return (liquidity_score 0..1, expected_slippage_bp, reason)."""
    components = []
    reason_parts = []

    # Book thinness component.
    top = float(book["top_depth_usd"]) if book and book["top_depth_usd"] else 0.0
    thin = max(0.0, 1.0 - min(top / TOP_DEPTH_FULL_USD, 1.0))
    components.append(("thinness", thin, 0.35))
    reason_parts.append(f"top_depth=${top:,.0f} thin={thin:.2f}")

    # Spread component: >= 30bp -> full risk, <= 5bp -> zero.
    spread = float(book["spread_bp"]) if book and book["spread_bp"] else 999.0
    spread_norm = min(max((spread - 5.0) / 25.0, 0.0), 1.0)
    components.append(("spread", spread_norm, 0.25))
    reason_parts.append(f"spread={spread:.1f}bp norm={spread_norm:.2f}")

    # Imbalance component (distance from 0.5 = equal).
    bid_d = float(book["bid_depth_usd"]) if book and book["bid_depth_usd"] else 0.0
    ask_d = float(book["ask_depth_usd"]) if book and book["ask_depth_usd"] else 0.0
    if bid_d + ask_d > 0:
        imb = bid_d / (bid_d + ask_d)
        imb_penalty = abs(imb - 0.5) * 2       # 0 = balanced, 1 = one-sided
    else:
        imb = None
        imb_penalty = 1.0
    components.append(("imbalance", imb_penalty, 0.10))
    reason_parts.append(f"imbalance={'?' if imb is None else f'{imb:.2f}'} penalty={imb_penalty:.2f}")

    # Own-flow slippage component (realized reality beats model).
    own_slip = own.get("slip_mean")
    if own_slip is None:
        slip_comp = 0.5        # unknown → medium risk
        reason_parts.append("realized_slip=UNKNOWN")
    else:
        # >= 20bp realized -> full risk, <= 2bp -> zero.
        slip_comp = min(max((own_slip - 2.0) / 18.0, 0.0), 1.0)
        reason_parts.append(f"realized_slip={own_slip:.1f}bp comp={slip_comp:.2f}")
    components.append(("realized_slip", slip_comp, 0.20))

    # Fill-rate component.
    own_fill = own.get("fill_mean")
    if own_fill is None:
        fill_comp = 0.3
    else:
        # fill_rate < 0.80 -> full risk, > 0.98 -> zero.
        fill_comp = min(max((0.98 - own_fill) / 0.18, 0.0), 1.0)
    components.append(("fill_rate", fill_comp, 0.10))
    if own_fill is not None:
        reason_parts.append(f"fill_rate={own_fill:.3f} comp={fill_comp:.2f}")

    total_weight = sum(w for _, _, w in components)
    score = sum(v * w for _, v, w in components) / total_weight
    score = min(max(score, 0.0), 1.0)

    # Expected slippage is the max of model and realized:
    #   model_bp ~ thinness*20 + spread*0.5  (rough)
    model_bp = thin * 20.0 + spread * 0.5
    if own_slip is not None:
        expected_bp = max(model_bp, own_slip)
    else:
        expected_bp = model_bp

    return round(score, 4), round(expected_bp, 2), " · ".join(reason_parts)


def reading_for(symbol: str) -> LiquidityReading:
    book = _book_state(symbol)
    own = _own_flow_stats(symbol)
    score, exp_bp, reason = _compute_score(book, own)
    return LiquidityReading(
        symbol=symbol,
        ts_ms=int(time.time() * 1000),
        bid_depth_usd=float(book["bid_depth_usd"]) if book and book["bid_depth_usd"] else None,
        ask_depth_usd=float(book["ask_depth_usd"]) if book and book["ask_depth_usd"] else None,
        top_depth_usd=float(book["top_depth_usd"]) if book and book["top_depth_usd"] else None,
        spread_bp=float(book["spread_bp"]) if book and book["spread_bp"] else None,
        book_imbalance=(
            float(book["bid_depth_usd"]) / (float(book["bid_depth_usd"]) + float(book["ask_depth_usd"]))
            if book and book["bid_depth_usd"] and book["ask_depth_usd"]
            and (float(book["bid_depth_usd"]) + float(book["ask_depth_usd"])) > 0
            else None
        ),
        thinness_score=round(
            max(0.0, 1.0 - min((float(book["top_depth_usd"]) if book and book["top_depth_usd"] else 0) / TOP_DEPTH_FULL_USD, 1.0)),
            4,
        ) if book else None,
        n_own_fills_24h=own["n"],
        realized_slippage_bp_mean=round(own["slip_mean"], 2) if own["slip_mean"] is not None else None,
        realized_fill_rate=round(own["fill_mean"], 4) if own["fill_mean"] is not None else None,
        liquidity_score=score,
        expected_slippage_bp=exp_bp,
        reason=reason,
    )


def should_block(symbol: str) -> tuple[bool, LiquidityReading]:
    """True iff gate_enabled AND score >= abort threshold.

    Advisory when gate is off: returns (False, reading) so callers can
    still inspect the reading without changing behaviour.
    """
    r = reading_for(symbol)
    if not gate_enabled():
        return False, r
    return (r.liquidity_score >= SCORE_ABORT_THRESHOLD), r
