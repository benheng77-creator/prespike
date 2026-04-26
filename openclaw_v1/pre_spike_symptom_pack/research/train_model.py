"""
Walk-forward LightGBM + isotonic calibration. Saves model_v{date}/ artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.model_selection import TimeSeriesSplit

# Pack root path shim — research/ scripts are invoked as top-level scripts
# via scripts/run_discovery.sh, not imported as a package. This shim ensures
# any cross-subpackage import (e.g. from pre_spike_symptom_pack.strategies.X
# import Y) resolves regardless of cwd or symlink path. Currently this script
# does not import from sibling subpackages, but the shim is included for
# consistency across the research/ directory and forward compatibility.
_PACK_ROOT = Path(__file__).resolve().parent.parent
if str(_PACK_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_PACK_ROOT.parent))



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", default="data/features")
    ap.add_argument("--out_root", default="models")
    args = ap.parse_args()

    dfs = [pd.read_parquet(f) for f in Path(args.features).glob("*.parquet")]
    df = pd.concat(dfs, ignore_index=True).sort_values("ts").reset_index(drop=True)
    feat_cols = [c for c in df.columns if c not in ("ts", "spike", "spike_direction")]
    X = df[feat_cols].fillna(0).values.astype(np.float32)
    y = df["spike"].values.astype(np.int32)

    # Walk-forward: last 17% = holdout
    n = len(X)
    holdout_start = int(n * 0.83)
    X_tr, y_tr = X[:holdout_start], y[:holdout_start]
    X_ho, y_ho = X[holdout_start:], y[holdout_start:]

    booster = lgb.train(
        {"objective": "binary", "metric": "average_precision",
         "learning_rate": 0.05, "num_leaves": 31,
         "is_unbalance": True, "verbose": -1},
        lgb.Dataset(X_tr, y_tr), num_boost_round=400,
    )

    # Calibrate via isotonic on a held-out validation slice from train tail
    from sklearn.isotonic import IsotonicRegression
    val_split = int(0.85 * len(X_tr))
    p_val = booster.predict(X_tr[val_split:])
    iso = IsotonicRegression(out_of_bounds="clip").fit(p_val, y_tr[val_split:])

    # Threshold tau: max F1 on validation, with precision >= 0.55
    from sklearn.metrics import precision_recall_curve
    p_cal_val = iso.transform(p_val)
    pr, rc, th = precision_recall_curve(y_tr[val_split:], p_cal_val)
    candidates = [(t, (2 * p * r / (p + r + 1e-12)))
                  for t, p, r in zip(th, pr[:-1], rc[:-1]) if p >= 0.55]
    if not candidates:
        raise RuntimeError("no threshold meets precision >= 0.55")
    tau = max(candidates, key=lambda x: x[1])[0]

    # Holdout single-shot
    p_ho = iso.transform(booster.predict(X_ho))
    yhat = (p_ho >= tau).astype(int)
    from sklearn.metrics import average_precision_score, roc_auc_score, brier_score_loss
    holdout = {
        "pr_auc": float(average_precision_score(y_ho, p_ho)),
        "roc_auc": float(roc_auc_score(y_ho, p_ho)),
        "brier": float(brier_score_loss(y_ho, p_ho)),
        "base_rate": float(y_ho.mean()),
        "n": int(len(y_ho)),
        "tau": float(tau),
    }

    # Persist artifact
    version = "model_v" + datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = Path(args.out_root) / version; out.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(out / "lightgbm_model.txt"))
    with open(out / "lightgbm_model.txt", "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()

    norm = {f: {"mu": float(np.mean(X_tr[:, i])),
                "sigma": float(np.std(X_tr[:, i]) + 1e-12)}
            for i, f in enumerate(feat_cols)}
    train_stats = {f: {"mu": norm[f]["mu"], "sigma": norm[f]["sigma"],
                       "p1": float(np.percentile(X_tr[:, i], 1)),
                       "p99": float(np.percentile(X_tr[:, i], 99))}
                   for i, f in enumerate(feat_cols)}
    with open(out / "feature_normalizer.json", "w") as f:
        json.dump(norm, f)
    with open(out / "threshold_tau.json", "w") as f:
        json.dump({"tau": float(tau)}, f)
    # Save isotonic
    import pickle
    with open(out / "isotonic_calibrator.pkl", "wb") as f:
        pickle.dump(iso, f)

    manifest = {
        "version": version,
        "feature_order": feat_cols,
        "sha256_lightgbm_model": sha,
        "holdout": holdout,
        "train_feature_stats": train_stats,
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    with open(out / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"artifact -> {out}\nholdout = {holdout}")


if __name__ == "__main__":
    main()
