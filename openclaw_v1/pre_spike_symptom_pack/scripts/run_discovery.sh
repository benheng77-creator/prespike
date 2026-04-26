#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Step 1: ingest history ==="
python research/ingest_history.py --out data/raw

echo "=== Step 2: label spikes ==="
python research/label_spikes.py --in data/raw --out data/labeled

echo "=== Step 3: compute features ==="
python research/compute_features.py --in data/labeled --out data/features

echo "=== Step 4: discover symptoms ==="
python research/discover_symptoms.py --in data/features --out research/reports

echo "=== Step 5: train model ==="
python research/train_model.py --features data/features --out_root models

echo "=== Step 6: evaluate holdout ==="
LATEST=$(ls -1d models/model_v* | sort | tail -1)
python research/evaluate_holdout.py "$LATEST"

echo "=== DONE — promote $LATEST in config/live.yaml approved_versions ==="
