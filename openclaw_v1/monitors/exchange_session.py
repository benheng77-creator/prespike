"""Exchange session monitor — checks ccxt exchange status."""

from __future__ import annotations

import inspect
from typing import Any, Optional

from ._base import BaseMonitor, MonitorResult


class ExchangeSessionMonitor(BaseMonitor):
    name = "exchange_session"

    def __init__(self, *, exchange: Any, ledger: Optional[Any] = None):
        super().__init__(ledger=ledger)
        self._ex = exchange

    async def check(self) -> MonitorResult:
        ex = self._ex
        if ex is None:
            return MonitorResult(name=self.name, ok=False, note="no exchange", severity="warn")
        try:
            fn = getattr(ex, "fetch_status", None)
            if callable(fn):
                r = fn()
                if inspect.isawaitable(r):
                    r = await r
                status = r.get("status") if isinstance(r, dict) else str(r)
                ok = status in ("ok", "OK", None)
                return MonitorResult(name=self.name, ok=ok, note=f"status={status}",
                                     severity="warn" if not ok else "info",
                                     detail={"raw": r if isinstance(r, dict) else {"raw": str(r)}})
            # fallback: probe a cheap call
            return MonitorResult(name=self.name, ok=True, note="no fetch_status")
        except Exception as e:
            return MonitorResult(name=self.name, ok=False, note=f"error: {e}",
                                 severity="error")
