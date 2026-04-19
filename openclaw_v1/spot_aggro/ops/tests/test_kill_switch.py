"""
Kill switch semantics. Uses the real DB schema but in a tmpdir so it
never touches the user's trades.db.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


class TestKillSwitch(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        os.environ["CLAW_DB_PATH"] = str(Path(self.tmp) / "t.db")
        os.environ["TRADE_DB_PATH"] = str(Path(self.tmp) / "t.db")
        # Override lock-file path so we don't touch the repo
        self._lock_override = Path(self.tmp) / "KILL_STATE.lock"
        # Import fresh after env patching
        import importlib, sys
        for mod in [m for m in list(sys.modules) if m.startswith("spot_aggro.ops")]:
            sys.modules.pop(mod)
        from spot_aggro.ops.risk import kill_switch
        self.ks = kill_switch
        # Monkeypatch the lock-path function to our tmpdir
        self.ks._lock_path = lambda: self._lock_override  # type: ignore[assignment]

    def test_no_trigger_below_threshold(self):
        kid = self.ks.check_and_trigger(equity_usd=95.0, peak_usd=100.0)
        self.assertIsNone(kid)
        self.assertFalse(self.ks.is_locked())

    def test_trigger_above_threshold(self):
        # 6% drawdown > 5% threshold
        kid = self.ks.check_and_trigger(equity_usd=94.0, peak_usd=100.0)
        self.assertIsNotNone(kid)
        self.assertTrue(self.ks.is_locked())

    def test_idempotent_trigger(self):
        self.ks.check_and_trigger(equity_usd=80.0, peak_usd=100.0)
        second = self.ks.check_and_trigger(equity_usd=70.0, peak_usd=100.0)
        self.assertIsNone(second)      # already locked
        self.assertTrue(self.ks.is_locked())

    def test_clear_unlocks(self):
        kid = self.ks.check_and_trigger(equity_usd=80.0, peak_usd=100.0)
        self.ks.clear_lock(kill_id=kid, operator="test", reason="unit")
        self.assertFalse(self.ks.is_locked())


if __name__ == "__main__":
    unittest.main()
