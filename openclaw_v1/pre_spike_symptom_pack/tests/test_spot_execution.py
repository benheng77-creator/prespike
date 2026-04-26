"""Spot execution behavioral tests with mock OKX client."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest


@dataclass
class _Tel:
    counters: dict
    gauges: dict
    def __init__(self): self.counters = {}; self.gauges = {}
    def inc(self, name, labels): self.counters[name] = self.counters.get(name, 0) + 1
    def gauge(self, name, value): self.gauges[name] = value


class _MockOKX:
    def __init__(self, balance=1000.0, bid=50000.0, ask=50001.0):
        self.balance = balance
        self.bid = bid; self.ask = ask
        self.orders = {}
        self.fail_post_only = False
    async def spot_get_balance(self, ccy): return self.balance
    async def spot_get_book_top(self, instId): return {"bid": self.bid, "ask": self.ask}
    async def spot_place_order(self, instId, side, ordType, sz, px=None, clOrdId=None):
        if ordType == "post_only" and self.fail_post_only:
            return {"status": "rejected_post_only"}
        oid = f"oxid-{len(self.orders)}"
        self.orders[clOrdId] = {"state": "filled", "avg_price": px or self.bid,
                                "filled_qty": sz, "fees": sz * (px or self.bid) * 0.0008,
                                "ord_id": oid}
        return {"status": "placed", "ord_id": oid, "client_oid": clOrdId}
    async def spot_query_order(self, instId, clOrdId):
        return self.orders.get(clOrdId, {"state": "live"})
    async def spot_cancel_order(self, instId, ordId): return {}


@pytest.mark.asyncio
async def test_rejects_without_audit_token():
    from services.spot_execution import SpotExecutionService
    svc = SpotExecutionService(_MockOKX(), _Tel())
    intent = {"instrument": "BTC-USDT", "audit_token": None,
              "provenance": {"atr_at_entry": 100.0}}
    res = await svc.submit_buy(intent, size_quote=50.0)
    assert not res.ok
    assert "audit" in res.reason


@pytest.mark.asyncio
async def test_rejects_insufficient_balance():
    from services.spot_execution import SpotExecutionService
    svc = SpotExecutionService(_MockOKX(balance=10.0), _Tel())
    intent = {"instrument": "BTC-USDT", "audit_token": "abc",
              "provenance": {"atr_at_entry": 100.0}}
    res = await svc.submit_buy(intent, size_quote=100.0)
    assert not res.ok
    assert "insufficient" in res.reason


@pytest.mark.asyncio
async def test_idempotent_client_oid():
    from services.spot_execution import SpotExecutionService
    okx = _MockOKX()
    svc = SpotExecutionService(okx, _Tel())
    intent = {"instrument": "BTC-USDT", "audit_token": "abc",
              "provenance": {"atr_at_entry": 100.0}}
    res = await svc.submit_buy(intent, size_quote=50.0)
    assert res.ok
    # client_oid format includes random hex; ensure it was used as a key
    assert any(coid.startswith("psp-") for coid in okx.orders.keys())
