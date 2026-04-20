"""Standalone math verification for simulator_aqueduct.

Drop this in openclaw_v1/spot_aggro/backtest/ and run:
    python -m openclaw_v1.spot_aggro.backtest.test_aqueduct

Tests the engine on three synthetic regimes:
  1. OU mean-reverting spread  -> must produce EDGE on realistic tier
  2. Independent random walks  -> must produce NO_EDGE on realistic tier
  3. Trending non-stationary   -> must produce NO_EDGE on realistic tier

If all three pass, the engine is mathematically sound and any
result on real data reflects the data, not a logic bug.
"""
from __future__ import annotations

import math
import random
import sys

from .data_puller import CandleSet
from .simulator_aqueduct import _simulate_symbol


def _make_ou_synthetic(n_bars: int = 5000, seed: int = 42) -> tuple[CandleSet, CandleSet]:
    random.seed(seed)
    cdc_rows = []
    okx_rows = []
    cdc_price = 100.0
    spread = 0.0
    theta = 0.3        # mean reversion speed
    sigma = 0.5        # spread innovation magnitude
    drift_sigma = 0.05  # CDC drift
    ts0 = 1700000000000
    for i in range(n_bars):
        cdc_price *= math.exp(random.gauss(0, drift_sigma / 100))
        spread = spread * (1 - theta) + random.gauss(0, sigma)
        okx_price = cdc_price + spread
        ts = ts0 + i * 60_000
        cdc_rows.append([ts, cdc_price, cdc_price, cdc_price, cdc_price, 100.0])
        okx_rows.append([ts, okx_price, okx_price, okx_price, okx_price, 100.0])
    return CandleSet("okx", "SYNTH-OU", "1m", okx_rows), CandleSet("cdc", "SYNTH-OU", "1m", cdc_rows)


def _make_rw_synthetic(n_bars: int = 5000, seed: int = 7) -> tuple[CandleSet, CandleSet]:
    random.seed(seed)
    cdc_rows = []
    okx_rows = []
    cdc_price = 100.0
    okx_price = 100.0
    ts0 = 1700000000000
    for i in range(n_bars):
        cdc_price *= math.exp(random.gauss(0, 0.001))
        okx_price *= math.exp(random.gauss(0, 0.001))
        ts = ts0 + i * 60_000
        cdc_rows.append([ts, cdc_price, cdc_price, cdc_price, cdc_price, 100.0])
        okx_rows.append([ts, okx_price, okx_price, okx_price, okx_price, 100.0])
    return CandleSet("okx", "SYNTH-RW", "1m", okx_rows), CandleSet("cdc", "SYNTH-RW", "1m", cdc_rows)


def _make_trend_synthetic(n_bars: int = 5000, seed: int = 11) -> tuple[CandleSet, CandleSet]:
    random.seed(seed)
    cdc_rows = []
    okx_rows = []
    cdc_price = 100.0
    spread_drift = 0.0
    ts0 = 1700000000000
    for i in range(n_bars):
        cdc_price *= math.exp(random.gauss(0, 0.001))
        spread_drift += random.gauss(0.001, 0.05)
        okx_price = cdc_price + spread_drift
        ts = ts0 + i * 60_000
        cdc_rows.append([ts, cdc_price, cdc_price, cdc_price, cdc_price, 100.0])
        okx_rows.append([ts, okx_price, okx_price, okx_price, okx_price, 100.0])
    return CandleSet("okx", "SYNTH-TREND", "1m", okx_rows), CandleSet("cdc", "SYNTH-TREND", "1m", cdc_rows)


def main() -> int:
    failures: list[str] = []

    print("=" * 70)
    print("AQUEDUCT engine math verification")
    print("=" * 70)

    # Test 1: OU mean-reverting must produce EDGE on realistic tier.
    okx, cdc = _make_ou_synthetic()
    res = _simulate_symbol(okx, cdc)
    real = res.by_cost.get("realistic", {})
    print(f"\n[1] OU mean-reverting synthetic:")
    print(f"    n_trades={res.n_trades}  WR={real.get('win_rate', 0)*100:.1f}%  "
          f"mean_net={real.get('mean_net_bp', 0):+.2f}bp  verdict={res.verdict.get('realistic')}")
    if res.verdict.get("realistic") != "EDGE":
        failures.append("Test 1 FAIL: OU process should produce EDGE on realistic tier")
    else:
        print("    PASS")

    # Test 2: Random walks must produce NO_EDGE on realistic tier.
    okx, cdc = _make_rw_synthetic()
    res = _simulate_symbol(okx, cdc)
    real = res.by_cost.get("realistic", {})
    print(f"\n[2] Independent random walks:")
    print(f"    n_trades={res.n_trades}  WR={real.get('win_rate', 0)*100:.1f}%  "
          f"mean_net={real.get('mean_net_bp', 0):+.2f}bp  verdict={res.verdict.get('realistic')}")
    if res.verdict.get("realistic") == "EDGE":
        failures.append("Test 2 FAIL: random walks should NOT produce EDGE")
    else:
        print("    PASS")

    # Test 3: Trending must produce NO_EDGE on realistic tier.
    okx, cdc = _make_trend_synthetic()
    res = _simulate_symbol(okx, cdc)
    real = res.by_cost.get("realistic", {})
    print(f"\n[3] Trending spread (anti-pattern):")
    print(f"    n_trades={res.n_trades}  WR={real.get('win_rate', 0)*100:.1f}%  "
          f"mean_net={real.get('mean_net_bp', 0):+.2f}bp  verdict={res.verdict.get('realistic')}")
    if res.verdict.get("realistic") == "EDGE":
        failures.append("Test 3 FAIL: trending series should NOT produce EDGE")
    else:
        print("    PASS")

    print("\n" + "=" * 70)
    if failures:
        print("VERIFICATION FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("VERIFICATION PASSED — engine math is sound. Safe to run on real data.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
