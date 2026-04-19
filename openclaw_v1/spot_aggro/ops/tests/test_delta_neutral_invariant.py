"""
Tests for Δ-neutrality invariant (spec §10 fact #1 — identity-true).
"""

from __future__ import annotations

import unittest

from spot_aggro.ops.risk.delta_neutrality_check import check


class TestDeltaNeutral(unittest.TestCase):

    def test_perfect_hedge_is_ok(self):
        r = check(symbol="INJ-USDT",
                  spot_qty=100.0, spot_px=3.35,
                  perp_contracts=-100.0, perp_mark=3.35)
        self.assertTrue(r.ok)
        self.assertAlmostEqual(r.delta_usd, 0.0, places=6)

    def test_mild_drift_ok_within_tolerance(self):
        # 100 spot @ 3.35, short 99.8 @ 3.35 — drift = 0.2 / 334.3 = 0.06%
        r = check(symbol="INJ-USDT",
                  spot_qty=100.0, spot_px=3.35,
                  perp_contracts=-99.8, perp_mark=3.35)
        self.assertTrue(r.ok)
        self.assertLess(r.drift_pct, r.tolerance)

    def test_drift_beyond_tolerance_flagged(self):
        # 100 spot, short only 90 — 10% drift, way beyond 0.5% tol
        r = check(symbol="INJ-USDT",
                  spot_qty=100.0, spot_px=3.35,
                  perp_contracts=-90.0, perp_mark=3.35)
        self.assertFalse(r.ok)
        self.assertGreater(r.drift_pct, r.tolerance)

    def test_long_long_is_naked(self):
        """Two longs is NOT delta-neutral — should fail."""
        r = check(symbol="INJ-USDT",
                  spot_qty=100.0, spot_px=3.35,
                  perp_contracts=+100.0, perp_mark=3.35)
        self.assertFalse(r.ok)


if __name__ == "__main__":
    unittest.main()
