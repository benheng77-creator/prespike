"""Monitor: upstream tick freshness. Read-only view of existing trackers."""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from ._base import BaseMonitor, MonitorResult


class DataFreshnessMonitor(BaseMonitor):
    name = "data_freshness"

    def __init__(
        self,
        *,
        last_ts_ms_getter: Callable[[], int],
        max_age_s: int = 30,
        source: str = "feed",
        ledger: Optional[Any] = None,
    ):
        super().__init__(ledger=ledger)
        self._getter = last_ts_ms_getter
        self.max_age_s = max_age_s
        self.source = source

    async def check(self) -> MonitorResult:
        try:
            last_ms = int(self._getter() or 0)
        except Exception as e:
            return MonitorResult(name=self.name, ok=False, note=f"getter error: {e}",
                                 severity="error")
        if last_ms <= 0:
            return MonitorResult(name=self.name, ok=False, note="no data yet",
                                 severity="warn", detail={"source": self.source})
        age_s = int(time.time() - last_ms / 1000.0)
        ok = age_s <= self.max_age_s
        return MonitorResult(
            name=self.name, ok=ok,
            note=f"{self.source}_age={age_s}s",
            severity="warn" if not ok else "info",
            detail={"source": self.source, "age_s": age_s, "max_age_s": self.max_age_s},
        )
