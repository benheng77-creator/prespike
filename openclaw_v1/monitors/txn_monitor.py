"""Transaction monitor — reconciles audit `filled` rows vs `finalized`."""

from __future__ import annotations

from typing import Any, Optional

from ._base import BaseMonitor, MonitorResult


class TxnMonitor(BaseMonitor):
    name = "txn_monitor"

    def __init__(self, *, ledger: Any, stale_minutes: int = 5):
        super().__init__(ledger=ledger)
        self._ledger = ledger
        self.stale_minutes = stale_minutes

    async def check(self) -> MonitorResult:
        # Recent intents that never reached finalized == stuck.
        rows = self._ledger.fetch_recent(limit=1000)
        by_cid: dict[str, list[str]] = {}
        for r in rows:
            by_cid.setdefault(r["correlation_id"], []).append(r["kind"])
        stuck = [cid for cid, kinds in by_cid.items()
                 if "intent" in kinds and "finalized" not in kinds]
        ok = not stuck
        return MonitorResult(
            name=self.name, ok=ok,
            note=f"stuck={len(stuck)}",
            severity="warn" if not ok else "info",
            detail={"stuck_correlation_ids": stuck[:20]},
        )
