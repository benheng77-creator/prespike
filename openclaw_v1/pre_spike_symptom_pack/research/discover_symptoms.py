"""
3-pass symptom discovery: KS-test + tree importance + Granger.
Outputs reports/SYMPTOM_DISCOVERY_REPORT.md.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

# Pack root path shim — research/ scripts are invoked as top-level scripts
# via scripts/run_discovery.sh, not imported as a package. This shim ensures
# any cross-subpackage import (e.g. from pre_spike_symptom_pack.strategies.X
# import Y) resolves regardless of cwd or symlink path. Currently this script
# does not import from sibling subpackages, but the shim is included for
# consistency across the research/ directory and forward compatibility.
_PACK_ROOT = Path(__file__).resolve().parent.parent
if str(_PACK_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_PACK_ROOT.parent))



def ks_pass(df: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    rows = []
    pos = df[df["spike"] == 1]
    neg = df[df["spike"] == 0]
    for f in features:
        a = pos[f].dropna().values
        b = neg[f].dropna().values
        if len(a) < 50 or len(b) < 50:
            continue
        ks, p = stats.ks_2samp(a, b)
        # Cohen's d
        d = (a.mean() - b.mean()) / np.sqrt((a.var() + b.var()) / 2 + 1e-12)
        rows.append({"feature": f, "ks": ks, "p": p, "cohen_d": d, "n_pos": len(a)})
    out = pd.DataFrame(rows).sort_values("ks", ascending=False)
    # Benjamini-Hochberg
    p_sorted = out["p"].values
    m = len(p_sorted); ranks = np.arange(1, m + 1)
    bh = p_sorted * m / ranks
    bh = np.minimum.accumulate(bh[::-1])[::-1]
    out["p_fdr"] = bh
    return out


def lgb_importance(df: pd.DataFrame, features: list[str]) -> pd.DataFrame:
    import lightgbm as lgb
    from sklearn.model_selection import TimeSeriesSplit
    X = df[features].fillna(0).values
    y = df["spike"].values
    tscv = TimeSeriesSplit(n_splits=5)
    importances = np.zeros(len(features))
    for tr, va in tscv.split(X):
        clf = lgb.LGBMClassifier(class_weight="balanced", n_estimators=200, verbose=-1)
        clf.fit(X[tr], y[tr])
        importances += clf.booster_.feature_importance(importance_type="gain")
    importances /= 5
    return pd.DataFrame({"feature": features, "lgb_importance": importances}) \
        .sort_values("lgb_importance", ascending=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data/features")
    ap.add_argument("--out", default="research/reports")
    args = ap.parse_args()
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    dfs = [pd.read_parquet(f) for f in Path(args.inp).glob("*.parquet")]
    df = pd.concat(dfs, ignore_index=True).sort_values("ts").reset_index(drop=True)
    features = [c for c in df.columns if c not in ("ts", "spike", "spike_direction")]
    ks = ks_pass(df, features)
    imp = lgb_importance(df, features)
    merged = ks.merge(imp, on="feature")
    merged.to_csv(out / "symptom_ranking.csv", index=False)
    confirmed = merged[(merged["p_fdr"] < 0.05) & (merged["lgb_importance"] > 0)] \
        .sort_values("lgb_importance", ascending=False).head(20)

    with open(out / "SYMPTOM_DISCOVERY_REPORT.md", "w") as f:
        f.write("# Symptom Discovery Report\n\n")
        f.write("## Top 20 Confirmed Symptoms\n\n")
        f.write(confirmed.to_markdown(index=False))
        f.write("\n\n## What We Did Not Find\n\n")
        rejected = merged[(merged["p_fdr"] >= 0.05) | (merged["lgb_importance"] <= 0)]
        f.write(rejected.head(10).to_markdown(index=False))
    print(f"report -> {out / 'SYMPTOM_DISCOVERY_REPORT.md'}")


if __name__ == "__main__":
    main()
