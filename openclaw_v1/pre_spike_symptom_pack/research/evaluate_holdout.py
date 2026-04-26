"""Single-shot holdout evaluation. Reads manifest.json from a model artifact."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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
    ap.add_argument("artifact_dir")
    args = ap.parse_args()
    with open(Path(args.artifact_dir) / "manifest.json") as f:
        m = json.load(f)
    h = m["holdout"]
    base = h["base_rate"]
    lift = h["pr_auc"] / max(base, 1e-9)
    print(f"PR-AUC = {h['pr_auc']:.4f}  base = {base:.4f}  lift = {lift:.2f}x")
    print(f"ROC-AUC = {h['roc_auc']:.4f}  Brier = {h['brier']:.4f}")
    if lift < 1.5:
        print("WARNING: PR-AUC lift < 1.5x base rate. Strategy NOT deployment-ready.")


if __name__ == "__main__":
    main()
