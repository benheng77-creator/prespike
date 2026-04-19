"""Common base for monitors. Minimal — they all share the same ledger write shape."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class MonitorResult:
    name: str
    ok: bool
    note: str = ""
    severity: str = "info"
    detail: dict = field(default_factory=dict)
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))


class BaseMonitor:
    name = "base"

    def __init__(self, *, ledger: Optional[Any] = None):
        self._ledger = ledger

    async def check(self) -> MonitorResult:  # pragma: no cover - override
        raise NotImplementedError

    async def __call__(self) -> MonitorResult:
        r = await self.check()
        if self._ledger is not None:
            self._ledger.record(
                kind="monitor",
                phase=self.name,
                severity="info" if r.ok else (r.severity or "warn"),
                result={"ok": r.ok, "note": r.note, "detail": r.detail},
            )
        return r
