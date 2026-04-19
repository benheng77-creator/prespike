"""
Phase D2β — Exchange → Engine reconciliation (SPOT AGGRO only).

Problem it solves:
    After a DB reset / fresh engine start, OKX still holds coins from
    prior sessions, but the engine's in-memory Position state is empty.
    The engine then tries to open NEW positions, which collide with the
    exchange's USDT shortage (all cash is tied up in the held coins), and
    every order returns 51008 INSUFFICIENT_FUNDS.

    Reconciliation pulls real OKX fills and rebuilds average cost basis
    per held coin, writing Position rows into the engine's state so the
    engine knows what it owns.

Reconstruction method (no invented prices):
    For each held coin:
      1. Pull fetch_my_trades(symbol, limit=200) via the spot adapter.
      2. Walk fills chronologically. Maintain (qty, cost_basis_usd).
         - buy  qty  → qty += amount   ; cost += amount * price
         - sell qty  → weighted-avg reduce:
             sold_cost = (amount / qty) * cost   (before subtraction)
             qty  -= amount
             cost -= sold_cost
      3. At the end, current_avg_price = cost / qty  (if qty > 0)
         current_entry_time = timestamp of LAST buy fill.
      4. Sanity check: |reconstructed_qty - actual_holding_qty| / actual <= 0.005
         (0.5% — accounts for OKX rounding, fee deductions denominated in
         the base coin that subtract silently).
         Pass → HIGH_CONFIDENCE. Fail → LOW_CONFIDENCE (still written but
         flagged; TP/SL still advisory).

Output: reconstructed Position dataclass instances, keyed by symbol.

Never consults capital thresholds. Never blocks anything. Pure read-only
on OKX + pure write into the engine's already-existing Position model.
Frozen forensic_v2/** is not touched.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from shared.adapters import OKXUnified, OKXError

log = logging.getLogger("spot_aggro.reconciliation")


# Minimum amount ratio tolerance for HIGH_CONFIDENCE. 0.5% covers OKX
# rounding + base-coin fee accounting. Anything larger is LOW_CONFIDENCE.
QTY_MATCH_TOLERANCE = 0.005

# Status codes — stable strings. Any new value needs a dashboard update.
STATUS_HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
STATUS_LOW_CONFIDENCE = "LOW_CONFIDENCE"
STATUS_RECONCILIATION_PENDING = "RECONCILIATION_PENDING"


@dataclass(frozen=True)
class ReconstructedPosition:
    """One reconciled spot holding. Maps 1:1 to the engine's `Position`
    dataclass, plus diagnostic fields the dashboard surfaces."""
    symbol: str                 # e.g. "TIA-USDT"
    entry_price: float          # weighted-average cost basis
    size_usd: float             # current qty * current mark price
    qty_held: float             # actual on-exchange quantity
    qty_reconstructed: float    # sum(buys) - sum(sells) from fills
    entry_time: float           # last-buy-fill unix seconds
    status: str                 # HIGH_CONFIDENCE | LOW_CONFIDENCE | PENDING
    n_fills: int
    n_buys: int
    n_sells: int
    qty_delta_pct: float        # |reconstructed - held| / held
    pending_reason: Optional[str]


def _rebuild_one(
    symbol_ccxt: str,          # "TIA/USDT"
    held_qty: float,
    fills: list[dict[str, Any]],
    current_price: float,
) -> ReconstructedPosition:
    """Walk fills chronologically, return a ReconstructedPosition.

    `symbol_ccxt` is the OKX ccxt-style symbol. We convert to the engine
    style ("TIA-USDT") for the output so it matches state.positions keys.
    """
    # engine-style symbol
    sym_engine = symbol_ccxt.replace("/", "-")

    if not fills:
        return ReconstructedPosition(
            symbol=sym_engine,
            entry_price=0.0, size_usd=0.0,
            qty_held=held_qty, qty_reconstructed=0.0,
            entry_time=0.0,
            status=STATUS_RECONCILIATION_PENDING,
            n_fills=0, n_buys=0, n_sells=0,
            qty_delta_pct=1.0,
            pending_reason="no fills returned by OKX",
        )

    # Sort chronologically. ccxt timestamps are ms.
    fills_sorted = sorted(fills, key=lambda f: f.get("timestamp") or 0)

    qty = 0.0
    cost_basis = 0.0
    last_buy_ts = 0
    n_buys = 0
    n_sells = 0
    for f in fills_sorted:
        side = (f.get("side") or "").lower()
        amt = float(f.get("amount") or 0)
        px = float(f.get("price") or 0)
        if amt <= 0 or px <= 0:
            continue
        if side == "buy":
            qty += amt
            cost_basis += amt * px
            n_buys += 1
            ts = int(f.get("timestamp") or 0)
            if ts > last_buy_ts:
                last_buy_ts = ts
        elif side == "sell":
            # Proportional cost-basis reduction
            if qty > 0:
                sold_cost = (amt / qty) * cost_basis if amt < qty else cost_basis
                cost_basis = max(0.0, cost_basis - sold_cost)
            qty = max(0.0, qty - amt)
            n_sells += 1
        # Unknown sides are ignored — fee-only fills etc.

    reconstructed_qty = qty
    if reconstructed_qty <= 0:
        return ReconstructedPosition(
            symbol=sym_engine,
            entry_price=0.0, size_usd=0.0,
            qty_held=held_qty, qty_reconstructed=0.0,
            entry_time=0.0,
            status=STATUS_RECONCILIATION_PENDING,
            n_fills=len(fills_sorted), n_buys=n_buys, n_sells=n_sells,
            qty_delta_pct=1.0,
            pending_reason="fills reconstruct to zero qty — sells exceeded buys in window",
        )

    # Qty match — compare reconstructed to actual holding.
    if held_qty <= 0:
        delta_pct = 1.0
    else:
        delta_pct = abs(reconstructed_qty - held_qty) / held_qty
    avg_entry = cost_basis / reconstructed_qty if reconstructed_qty > 0 else 0.0
    size_usd = held_qty * current_price if current_price > 0 else 0.0

    status = (
        STATUS_HIGH_CONFIDENCE if delta_pct <= QTY_MATCH_TOLERANCE
        else STATUS_LOW_CONFIDENCE
    )
    pending_reason = None
    if status == STATUS_LOW_CONFIDENCE:
        pending_reason = (
            f"reconstructed qty {reconstructed_qty:.6f} vs held {held_qty:.6f} "
            f"(delta {delta_pct*100:.2f}% > {QTY_MATCH_TOLERANCE*100:.2f}%) — "
            f"fill history may be truncated or contain fee-only rows"
        )

    entry_time = (last_buy_ts / 1000.0) if last_buy_ts else time.time()

    return ReconstructedPosition(
        symbol=sym_engine,
        entry_price=round(avg_entry, 8),
        size_usd=round(size_usd, 4),
        qty_held=held_qty,
        qty_reconstructed=round(reconstructed_qty, 8),
        entry_time=entry_time,
        status=status,
        n_fills=len(fills_sorted),
        n_buys=n_buys,
        n_sells=n_sells,
        qty_delta_pct=round(delta_pct, 6),
        pending_reason=pending_reason,
    )


async def reconcile_from_exchange(
    adapter: OKXUnified,
    *,
    fills_limit: int = 100,   # OKX spot fills API max
) -> tuple[dict[str, ReconstructedPosition], dict[str, Any]]:
    """Pull holdings + per-symbol fills, reconstruct all positions.

    Returns:
        (positions_by_symbol, summary_dict)

    summary_dict shape:
        {
          "ts": unix_seconds,
          "n_holdings": int,
          "n_high_confidence": int,
          "n_low_confidence": int,
          "n_pending": int,
          "free_usdt": float,
          "total_holdings_usd": float,
          "reasons": [per-symbol list of dict summaries],
        }

    Never raises on per-symbol fill failures — a broken symbol becomes
    RECONCILIATION_PENDING and the rest proceed.
    """
    holdings = await adapter.get_spot_holdings()
    try:
        free_usdt = await adapter.get_free_usdt()
    except Exception:
        free_usdt = None

    out: dict[str, ReconstructedPosition] = {}
    reasons: list[dict[str, Any]] = []
    total_holdings_usd = 0.0

    for coin, qty in holdings.items():
        sym_engine = f"{coin}-USDT"
        sym_ccxt = f"{coin}/USDT"
        # Current mark price — needed for size_usd; also detects stale
        # symbols where the ticker fails.
        try:
            ticker = await adapter.get_spot_ticker(sym_engine)
            current_price = float(ticker.get("last") or 0)
        except Exception as exc:
            log.warning("reconcile: ticker failed for %s: %s", sym_engine, exc)
            current_price = 0.0

        fills: list[dict[str, Any]] = []
        fetch_err: Optional[Exception] = None
        # Try twice: OKX rate-limits tightly and a retry after 250ms
        # usually succeeds if the first attempt was throttled.
        for attempt in range(2):
            try:
                fills = await adapter.get_spot_fills(sym_engine, limit=fills_limit)
                fetch_err = None
                break
            except Exception as exc:
                fetch_err = exc
                if attempt == 0:
                    await asyncio.sleep(0.25)
        if fetch_err is not None:
            exc = fetch_err
            log.warning("reconcile: fills failed for %s: %s", sym_engine, exc)
            rec = ReconstructedPosition(
                symbol=sym_engine,
                entry_price=0.0, size_usd=qty * current_price,
                qty_held=qty, qty_reconstructed=0.0,
                entry_time=0.0,
                status=STATUS_RECONCILIATION_PENDING,
                n_fills=0, n_buys=0, n_sells=0,
                qty_delta_pct=1.0,
                pending_reason=f"fills fetch failed: {type(exc).__name__}: {str(exc)[:160]}",
            )
            out[sym_engine] = rec
            reasons.append({
                "symbol": sym_engine, "status": rec.status,
                "n_fills": 0, "n_buys": 0, "n_sells": 0,
                "qty_held": qty, "qty_reconstructed": 0.0,
                "delta_pct": 1.0, "reason": rec.pending_reason,
            })
            total_holdings_usd += qty * current_price
            continue

        rec = _rebuild_one(sym_ccxt, qty, fills, current_price)
        out[sym_engine] = rec
        reasons.append({
            "symbol": sym_engine, "status": rec.status,
            "n_fills": rec.n_fills, "n_buys": rec.n_buys, "n_sells": rec.n_sells,
            "qty_held": rec.qty_held,
            "qty_reconstructed": rec.qty_reconstructed,
            "delta_pct": rec.qty_delta_pct,
            "entry_price": rec.entry_price,
            "reason": rec.pending_reason,
        })
        total_holdings_usd += rec.size_usd
        # Inter-symbol pause — OKX rate limits spot read endpoints at ~20 req/s
        # per UID. 150ms keeps us well under that envelope even with the
        # 2-call-per-symbol pattern (ticker + fills).
        await asyncio.sleep(0.15)

    n_hi = sum(1 for r in out.values() if r.status == STATUS_HIGH_CONFIDENCE)
    n_lo = sum(1 for r in out.values() if r.status == STATUS_LOW_CONFIDENCE)
    n_pd = sum(1 for r in out.values() if r.status == STATUS_RECONCILIATION_PENDING)

    summary = {
        "ts": time.time(),
        "n_holdings": len(holdings),
        "n_high_confidence": n_hi,
        "n_low_confidence": n_lo,
        "n_pending": n_pd,
        "free_usdt": round(free_usdt, 6) if free_usdt is not None else None,
        "total_holdings_usd": round(total_holdings_usd, 2),
        "reasons": reasons,
    }
    log.info(
        "[reconcile] holdings=%d hi=%d lo=%d pending=%d free_usdt=%s",
        len(holdings), n_hi, n_lo, n_pd,
        f"${free_usdt:.2f}" if free_usdt is not None else "unknown",
    )
    return out, summary
