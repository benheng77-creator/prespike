"""
Download 3 years of Binance bars for the configured instruments.
Output: parquet files in data/raw/{symbol}.parquet
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# Pack root path shim — research/ scripts are invoked as top-level scripts
# via scripts/run_discovery.sh, not imported as a package. This shim ensures
# any cross-subpackage import (e.g. from pre_spike_symptom_pack.strategies.X
# import Y) resolves regardless of cwd or symlink path. Currently this script
# does not import from sibling subpackages, but the shim is included for
# consistency across the research/ directory and forward compatibility.
_PACK_ROOT = Path(__file__).resolve().parent.parent
if str(_PACK_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_PACK_ROOT.parent))



def fetch_klines(symbol: str, start_ms: int, end_ms: int, interval: str = "1m") -> pd.DataFrame:
    """Stub fetcher — wire to your existing Binance REST client."""
    raise NotImplementedError(
        "Wire this to your existing Binance REST kline client. "
        "Expected return: DataFrame with columns "
        "[ts, open, high, low, close, volume]. "
        "Timestamps in UTC milliseconds."
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/raw")
    ap.add_argument("--symbols", nargs="+",
                    default=["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"])
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--interval", default="1m")
    args = ap.parse_args()

    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    end = datetime.now(timezone.utc) - timedelta(days=7)
    start = end - timedelta(days=int(365 * args.years))
    for sym in args.symbols:
        df = fetch_klines(sym, int(start.timestamp() * 1000),
                          int(end.timestamp() * 1000), args.interval)
        df.to_parquet(out / f"{sym}.parquet")
        print(f"saved {sym}: {len(df)} rows -> {out / f'{sym}.parquet'}")


if __name__ == "__main__":
    main()
