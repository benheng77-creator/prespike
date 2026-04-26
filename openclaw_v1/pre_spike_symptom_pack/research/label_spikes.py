"""
Forward-window spike labeling.
spike(t) = 1 iff max forward |move|/ATR within H bars >= M.
"""
from __future__ import annotations

import argparse
import sys
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



def label(df: pd.DataFrame, atr_period: int = 14, H: int = 24,
          M: float = 2.5) -> pd.DataFrame:
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    prev_c = np.roll(c, 1); prev_c[0] = c[0]
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)])
    atr = pd.Series(tr).rolling(atr_period, min_periods=atr_period).mean().values

    n = len(c)
    spike = np.zeros(n, dtype=np.int8)
    direction = np.zeros(n, dtype=np.int8)
    for i in range(n - H):
        if not np.isfinite(atr[i]) or atr[i] <= 0:
            continue
        fwd_h = np.max(h[i + 1:i + 1 + H])
        fwd_l = np.min(l[i + 1:i + 1 + H])
        up = (fwd_h - c[i]) / atr[i]
        dn = (c[i] - fwd_l) / atr[i]
        if max(up, dn) >= M:
            spike[i] = 1
            direction[i] = 1 if up >= dn else -1

    # Deconflict: keep only first of consecutive spike-positive cluster
    keep = np.zeros(n, dtype=np.int8)
    prev = 0
    for i in range(n):
        if spike[i] == 1 and prev == 0:
            keep[i] = 1
        prev = spike[i]
    df = df.copy()
    df["spike"] = keep
    df["spike_direction"] = direction * keep
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data/raw")
    ap.add_argument("--out", default="data/labeled")
    ap.add_argument("--H", type=int, default=24)
    ap.add_argument("--M", type=float, default=2.5)
    args = ap.parse_args()
    inp = Path(args.inp); out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    for f in inp.glob("*.parquet"):
        df = pd.read_parquet(f)
        df = label(df, H=args.H, M=args.M)
        df.to_parquet(out / f.name)
        print(f"{f.name}: {df['spike'].sum()} spikes / {len(df)} bars "
              f"({100*df['spike'].mean():.2f}%)")


if __name__ == "__main__":
    main()
