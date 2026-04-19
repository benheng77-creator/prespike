"""
Shared pre-trade risk gate.

Thin, import-safe wrapper for every model. Both interval and tick engines
call `check_pre_trade(context)` before placing a new quote or hedge.
"""

from __future__ import annotations

import os
from typing import Any


_MIN_EDGE = float(os.environ.get("SHARED_MIN_EDGE", "0.005"))
_F_MAX = float(os.environ.get("SHARED_F_MAX", "0.05"))
_FILL_RATE_FLOOR = float(os.environ.get("SHARED_FILL_RATE_FLOOR", "0.75"))
_LATENCY_BOUND_MS = float(os.environ.get("SHARED_LATENCY_BOUND_MS", "200"))
_DRAWDOWN_STOP = float(os.environ.get("SHARED_DRAWDOWN_STOP", "0.20"))


def check_pre_trade(context: dict[str, Any]) -> tuple[bool, str]:
    """
    Consult the risk gate. Returns (ok, reason). Context may include:
        edge, size_pct, fill_rate, latency_ms, drawdown, halted
    """
    if context.get("halted"):
        return False, "halted"

    edge = context.get("edge")
    if edge is not None and float(edge) < _MIN_EDGE and context.get("strict_edge", False):
        return False, f"edge<{_MIN_EDGE}"

    size_pct = context.get("size_pct")
    if size_pct is not None and float(size_pct) > _F_MAX:
        return False, f"size_pct>{_F_MAX}"

    fr = context.get("fill_rate")
    if fr is not None and float(fr) < _FILL_RATE_FLOOR:
        return False, f"fill_rate<{_FILL_RATE_FLOOR}"

    lat = context.get("latency_ms")
    if lat is not None and float(lat) > _LATENCY_BOUND_MS:
        return False, f"latency>{_LATENCY_BOUND_MS}ms"

    dd = context.get("drawdown")
    if dd is not None and float(dd) > _DRAWDOWN_STOP:
        return False, f"drawdown>{_DRAWDOWN_STOP}"

    inv = context.get("inventory_base")
    inv_limit = context.get("inventory_limit")
    if inv is not None and inv_limit is not None and abs(float(inv)) > float(inv_limit):
        return False, f"inventory>{inv_limit}"

    return True, "ok"
