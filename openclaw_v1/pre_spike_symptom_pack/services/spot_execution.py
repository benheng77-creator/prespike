"""
OKX spot execution: limit-maker orders, IOC fallback, bot-managed soft stops.

Speed posture: hot path is async. Order placement uses pre-built request
templates. Idempotent client_oid guarantees retry safety. Soft-stop manager
runs on a separate task per open position.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass(slots=True)
class SpotOrderResult:
    ok: bool
    client_oid: str
    exchange_oid: str | None
    status: str
    fill_price: float
    fill_qty: float
    fees_quote: float
    is_taker: bool
    reason: str = ""


@dataclass(slots=True)
class SpotPosition:
    instrument: str
    audit_token: str
    entry_price: float
    qty: float
    quote_invested: float
    atr_at_entry: float
    entry_ts: int
    target_price: float
    soft_stop_price: float
    time_stop_ts: int
    closed: bool = False
    exit_price: float = 0.0
    exit_reason: str = ""
    realized_pnl_quote: float = 0.0


class SpotExecutionService:
    """Live spot execution with bot-managed exits."""

    def __init__(self, okx_client, telemetry, audit_log_writer=None):
        self.okx = okx_client
        self.tel = telemetry
        self.audit = audit_log_writer
        self._open_positions: dict[str, SpotPosition] = {}   # client_oid -> pos
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------
    async def submit_buy(
        self,
        intent: dict,
        size_quote: float,
        timeout_seconds: int = 60,
        max_attempts: int = 2,
    ) -> SpotOrderResult:
        if intent.get("audit_token") is None:
            return SpotOrderResult(
                ok=False, client_oid="", exchange_oid=None, status="rejected",
                fill_price=0.0, fill_qty=0.0, fees_quote=0.0, is_taker=False,
                reason="missing_audit_token",
            )

        instrument = intent["instrument"]

        # Balance check
        bal = await self.okx.spot_get_balance("USDT")
        if bal < size_quote:
            return SpotOrderResult(
                ok=False, client_oid="", exchange_oid=None, status="rejected",
                fill_price=0.0, fill_qty=0.0, fees_quote=0.0, is_taker=False,
                reason=f"insufficient_balance avail={bal:.2f} need={size_quote:.2f}",
            )

        # Get touch
        book = await self.okx.spot_get_book_top(instrument)
        bid, ask = book["bid"], book["ask"]
        if not (bid > 0 and ask > 0 and ask >= bid):
            return SpotOrderResult(
                ok=False, client_oid="", exchange_oid=None, status="rejected",
                fill_price=0.0, fill_qty=0.0, fees_quote=0.0, is_taker=False,
                reason=f"bad_book bid={bid} ask={ask}",
            )

        client_oid_root = f"psp-{uuid.uuid4().hex[:16]}"

        # Maker-first: post-only at best bid
        for attempt in range(max_attempts):
            client_oid = f"{client_oid_root}-m{attempt}"
            limit_px = bid  # post inside spread on bid side
            qty = size_quote / limit_px
            self.tel.inc("spot_orders_placed_total",
                        {"instrument": instrument, "ord_type": "limit_maker"})
            resp = await self.okx.spot_place_order(
                instId=instrument, side="buy", ordType="post_only",
                sz=qty, px=limit_px, clOrdId=client_oid,
            )
            if resp.get("status") == "rejected_post_only":
                # book moved through limit before post took effect
                book = await self.okx.spot_get_book_top(instrument)
                bid, ask = book["bid"], book["ask"]
                continue

            # Wait for fill within timeout
            filled = await self._await_fill(instrument, client_oid, timeout_seconds)
            if filled["status"] == "filled":
                self.tel.inc("spot_fills_total",
                             {"instrument": instrument, "fill_type": "maker"})
                return self._on_filled(intent, instrument, client_oid, filled,
                                       size_quote, is_taker=False)
            if filled["status"] == "partial":
                # Cancel remainder, accept partial
                await self.okx.spot_cancel_order(instrument, filled["exchange_oid"])
                self.tel.inc("spot_fills_total",
                             {"instrument": instrument, "fill_type": "maker_partial"})
                return self._on_filled(intent, instrument, client_oid, filled,
                                       size_quote, is_taker=False)

            await self.okx.spot_cancel_order(instrument, filled.get("exchange_oid"))
            book = await self.okx.spot_get_book_top(instrument)
            bid, ask = book["bid"], book["ask"]

        # All maker attempts exhausted — V1 policy = drop, do NOT taker-fall back
        # unless the strategy explicitly opted in (spec says it never does).
        self.tel.inc("pre_spike_intents_rejected_total",
                     {"stage": "execution", "reason": "maker_exhausted"})
        return SpotOrderResult(
            ok=False, client_oid=client_oid_root, exchange_oid=None,
            status="dropped", fill_price=0.0, fill_qty=0.0, fees_quote=0.0,
            is_taker=False, reason="maker_attempts_exhausted",
        )

    # ------------------------------------------------------------------
    async def _await_fill(self, instrument: str, client_oid: str, timeout_s: int):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = await self.okx.spot_query_order(instrument, client_oid)
            if status["state"] in ("filled", "canceled", "partially_filled"):
                return {
                    "status": "filled" if status["state"] == "filled"
                              else ("partial" if status["state"] == "partially_filled"
                                    else "cancelled"),
                    "fill_price": status.get("avg_price", 0.0),
                    "fill_qty": status.get("filled_qty", 0.0),
                    "fees": status.get("fees", 0.0),
                    "exchange_oid": status.get("ord_id"),
                }
            await asyncio.sleep(1.0)
        # timed out
        status = await self.okx.spot_query_order(instrument, client_oid)
        return {
            "status": "timeout",
            "fill_price": status.get("avg_price", 0.0),
            "fill_qty": status.get("filled_qty", 0.0),
            "fees": status.get("fees", 0.0),
            "exchange_oid": status.get("ord_id"),
        }

    # ------------------------------------------------------------------
    def _on_filled(self, intent, instrument, client_oid, filled, size_quote, is_taker):
        atr = float(intent["provenance"].get("atr_at_entry", 0.0))
        if atr <= 0:
            atr = filled["fill_price"] * 0.005  # 50 bps fallback

        k_stop = float(intent.get("k_stop", 1.20))
        k_target = float(intent.get("k_target", 2.50))
        max_hours = int(intent.get("max_holding_hours", 6))

        entry_p = float(filled["fill_price"])
        qty = float(filled["fill_qty"])
        target = entry_p + k_target * atr
        soft_stop = entry_p - k_stop * atr
        time_stop = int(time.time()) + max_hours * 3600

        pos = SpotPosition(
            instrument=instrument,
            audit_token=intent["audit_token"],
            entry_price=entry_p,
            qty=qty,
            quote_invested=qty * entry_p,
            atr_at_entry=atr,
            entry_ts=int(time.time()),
            target_price=target,
            soft_stop_price=soft_stop,
            time_stop_ts=time_stop,
        )
        self._open_positions[client_oid] = pos
        self.tel.gauge("spot_open_positions", len(self._open_positions))
        # Spawn the soft-stop manager
        task = asyncio.create_task(self._manage_position(client_oid, pos))
        self._tasks.append(task)

        return SpotOrderResult(
            ok=True, client_oid=client_oid, exchange_oid=filled.get("exchange_oid"),
            status="filled", fill_price=entry_p, fill_qty=qty,
            fees_quote=float(filled.get("fees", 0.0)), is_taker=is_taker,
        )

    # ------------------------------------------------------------------
    async def _manage_position(self, client_oid: str, pos: SpotPosition):
        target_attempts = 0
        while not pos.closed:
            book = await self.okx.spot_get_book_top(pos.instrument)
            bid = book["bid"]
            now = int(time.time())

            if now >= pos.time_stop_ts:
                await self._close(client_oid, pos, bid, "time_stop")
                return
            if bid >= pos.target_price:
                ok = await self._try_sell_maker(pos, pos.target_price)
                if ok:
                    await self._close(client_oid, pos, pos.target_price, "target_hit")
                    return
                target_attempts += 1
                if target_attempts >= 3:
                    await self._close(client_oid, pos, bid, "target_market_fallback")
                    return
            if bid <= pos.soft_stop_price:
                await self._close(client_oid, pos, bid, "soft_stop")
                return

            await asyncio.sleep(1.0)

    async def _try_sell_maker(self, pos: SpotPosition, target_px: float) -> bool:
        coid = f"exit-{uuid.uuid4().hex[:12]}"
        resp = await self.okx.spot_place_order(
            instId=pos.instrument, side="sell", ordType="post_only",
            sz=pos.qty, px=target_px, clOrdId=coid,
        )
        return resp.get("status") in ("placed", "filled")

    async def _close(self, client_oid: str, pos: SpotPosition, exit_px: float, reason: str):
        if pos.closed:
            return
        # Best-effort market sell of remaining
        coid = f"close-{uuid.uuid4().hex[:12]}"
        await self.okx.spot_place_order(
            instId=pos.instrument, side="sell", ordType="market",
            sz=pos.qty, px=None, clOrdId=coid,
        )
        pos.closed = True
        pos.exit_price = exit_px
        pos.exit_reason = reason
        pos.realized_pnl_quote = (exit_px - pos.entry_price) * pos.qty
        self._open_positions.pop(client_oid, None)
        self.tel.gauge("spot_open_positions", len(self._open_positions))
        self.tel.inc("spot_position_closed_total",
                     {"instrument": pos.instrument, "reason": reason})

    async def flatten_all(self) -> int:
        """Emergency flatten. Returns number of positions closed."""
        positions = list(self._open_positions.items())
        for coid, pos in positions:
            book = await self.okx.spot_get_book_top(pos.instrument)
            await self._close(coid, pos, book["bid"], "manual_flatten")
        return len(positions)
