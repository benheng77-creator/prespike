"""Aqueduct backtest runner — invoke from repo root.

Usage:
    python -m openclaw_v1.spot_aggro.backtest.run_aqueduct \
        --days 30 --bar 1m

Or shorter for a quick smoke test (uses cached 2.4 days):
    python -m openclaw_v1.spot_aggro.backtest.run_aqueduct --days 2.4

Env-overridable tunables:
    AQDT_Z_ENTRY=2.5  AQDT_Z_EXIT=0.5  AQDT_Z_STOP=4.0
    AQDT_ROLL=60      AQDT_MAX_HOLD=30
    AQDT_NOTIONAL=5   AQDT_MAX_CONC=3
"""
from __future__ import annotations

import argparse
import sys

from .simulator_aqueduct import (
    print_report, run_aqueduct_backtest, save_aqueduct_report,
)


DEFAULT_UNIVERSE = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "SUI-USDT",
    "INJ-USDT", "SEI-USDT", "DOGE-USDT", "WIF-USDT",
    "PEPE-USDT", "ARB-USDT", "DOT-USDT", "ADA-USDT",
]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Aqueduct cross-exchange backtest")
    p.add_argument("--days", type=float, default=30.0,
                   help="Days of history to fetch (default 30)")
    p.add_argument("--bar", type=str, default="1m",
                   help="Bar size: 1m | 5m | 15m (default 1m)")
    p.add_argument("--symbols", type=str, default=",".join(DEFAULT_UNIVERSE),
                   help="Comma-separated universe (default 12-symbol live list)")
    p.add_argument("--force-refresh", action="store_true",
                   help="Bypass disk cache and re-pull all candles")
    p.add_argument("--label", type=str, default="aqueduct_30d",
                   help="Report filename label")
    args = p.parse_args(argv)

    universe = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not universe:
        print("ERROR: empty universe", file=sys.stderr)
        return 2

    print(f"[aqueduct] universe={len(universe)} symbols  "
          f"days={args.days}  bar={args.bar}  refresh={args.force_refresh}")

    report = run_aqueduct_backtest(
        universe=universe, days=args.days, bar=args.bar,
        force_refresh=args.force_refresh,
    )
    path = save_aqueduct_report(report, label=args.label)
    print_report(report)
    print(f"\nReport saved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
