"""Account balance drift vs expected (paper / ledger)."""

from __future__ import annotations

from typing import Any, Callable, Optional

from ._base import BaseMonitor, MonitorResult


class BalanceMonitor(BaseMonitor):
    name = "balance_monitor"

    def __init__(
        self,
        *,
        expected_balance_fn: Callable[[], float],
        observed_balance_fn: Callable[[], float],
        tolerance_frac: float = 0.005,
        ledger: Optional[Any] = None,
    ):
        super().__init__(ledger=ledger)
        self._expected = expected_balance_fn
        self._observed = observed_balance_fn
        self.tolerance_frac = tolerance_frac

    async def check(self) -> MonitorResult:
        try:
            expected = float(self._expected())
            observed = float(self._observed())
        except Exception as e:
            return MonitorResult(name=self.name, ok=False, note=f"getter error: {e}",
                                 severity="error")
        if expected == 0 and observed == 0:
            return MonitorResult(name=self.name, ok=True, note="both zero")
        diff = abs(expected - observed)
        base = max(abs(expected), abs(observed))
        frac = diff / base if base else 0
        ok = frac <= self.tolerance_frac
        return MonitorResult(
            name=self.name, ok=ok,
            note=f"drift_frac={frac:.4f}",
            severity="warn" if not ok else "info",
            detail={"expected": expected, "observed": observed, "diff": diff,
                    "frac": frac, "tolerance_frac": self.tolerance_frac},
        )
