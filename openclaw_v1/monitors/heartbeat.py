"""Loop heartbeat monitor — touches a cache file, reads age."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

from ._base import BaseMonitor, MonitorResult


class HeartbeatMonitor(BaseMonitor):
    name = "heartbeat"

    def __init__(
        self,
        *,
        heartbeat_path: str = "cache/heartbeat",
        max_age_s: int = 60,
        ledger: Optional[Any] = None,
    ):
        super().__init__(ledger=ledger)
        self.path = heartbeat_path
        self.max_age_s = max_age_s

    def tick(self) -> None:
        p = Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(int(time.time() * 1000)), encoding="utf-8")

    async def check(self) -> MonitorResult:
        p = Path(self.path)
        if not p.exists():
            return MonitorResult(name=self.name, ok=False, note="no heartbeat file",
                                 severity="warn", detail={"path": str(p)})
        try:
            last_ms = int(p.read_text(encoding="utf-8").strip())
        except Exception:
            return MonitorResult(name=self.name, ok=False, note="unreadable",
                                 severity="warn", detail={"path": str(p)})
        age_s = int(time.time() - last_ms / 1000.0)
        ok = age_s <= self.max_age_s
        return MonitorResult(
            name=self.name, ok=ok,
            note=f"age={age_s}s",
            severity="warn" if not ok else "info",
            detail={"age_s": age_s, "max_age_s": self.max_age_s},
        )
