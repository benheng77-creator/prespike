"""
PnL consistency — verify Σ(closed trade PnL) ≈ portfolio balance - starting.
"""

from __future__ import annotations

from typing import Any, Optional

from ._base import BaseMonitor, MonitorResult


class PnLConsistencyMonitor(BaseMonitor):
    name = "pnl_consistency"

    def __init__(
        self,
        *,
        portfolio_adapter: Any,
        persistence_adapter: Optional[Any] = None,
        tolerance: float = 1e-6,
        ledger: Optional[Any] = None,
    ):
        super().__init__(ledger=ledger)
        self._pf = portfolio_adapter
        self._ps = persistence_adapter
        self.tolerance = tolerance

    async def check(self) -> MonitorResult:
        snap = self._pf.snapshot()
        bal = float(snap.get("balance") or 0)
        start = float(snap.get("starting_balance") or 0)
        delta_balance = bal - start

        closed = self._pf.recent_closed(limit=10_000)
        sum_pnl = sum(float(t.get("pnl") or t.get("pnl_quote") or 0) for t in closed)

        diff = abs(delta_balance - sum_pnl)
        ok = diff <= max(self.tolerance, 1e-6)
        return MonitorResult(
            name=self.name, ok=ok,
            note=f"delta_balance={delta_balance:.4f} sum_pnl={sum_pnl:.4f} diff={diff:.4f}",
            severity="warn" if not ok else "info",
            detail={"delta_balance": delta_balance, "sum_pnl": sum_pnl, "diff": diff},
        )
