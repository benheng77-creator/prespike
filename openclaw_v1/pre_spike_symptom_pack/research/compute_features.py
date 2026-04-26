"""
Build the full feature matrix for the labeled bar set, using the canonical
research feature module.
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
# import Y) resolves regardless of cwd or symlink path. This script imports
# from sibling subpackage strategies/ — the shim is functionally required.
_PACK_ROOT = Path(__file__).resolve().parent.parent
if str(_PACK_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_PACK_ROOT.parent))

from pre_spike_symptom_pack.strategies._features_research import compute_features_research  # noqa: E402


def build_matrix(df: pd.DataFrame) -> pd.DataFrame:
    arr = df[["ts", "open", "high", "low", "close", "volume"]].values.astype(np.float64)
    rows = []
    for i in range(200, len(arr)):
        feats = compute_features_research(arr[: i + 1], feature_config={})
        rows.append({"ts": arr[i, 0], **feats})
    out = pd.DataFrame(rows)
    out = out.merge(df[["ts", "spike", "spike_direction"]], on="ts", how="left")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data/labeled")
    ap.add_argument("--out", default="data/features")
    args = ap.parse_args()
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    for f in Path(args.inp).glob("*.parquet"):
        df = pd.read_parquet(f)
        feat = build_matrix(df)
        feat.to_parquet(out / f.name)
        print(f"{f.name}: {feat.shape}")


if __name__ == "__main__":
    main()
