from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd


def die(reason: str, detail: str = "") -> None:
    payload = {
        "event": "PREFLIGHT_FAIL",
        "reason": reason,
        "detail": detail,
        "ts_iso": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    print(json.dumps(payload, default=str))
    raise SystemExit(1)


def ok(event: str, **kw) -> None:
    payload = {
        "event": event,
        "ts_iso": dt.datetime.now(dt.timezone.utc).isoformat(),
        **kw,
    }
    print(json.dumps(payload, default=str))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_root() -> Path:
    candidates = [
        Path.cwd(),
        Path(__file__).resolve().parents[2],  # openclaw_v1 from scanner file
        Path(__file__).resolve().parents[3],  # trading-bot fallback
    ]

    for root in candidates:
        if (root / "models").exists() and (root / "data" / "features").exists():
            return root

    die(
        "root_not_found",
        "Could not find root containing both models/ and data/features/. Run from openclaw_v1.",
    )


def latest_model(models_dir: Path) -> Path:
    dirs = sorted(
        [p for p in models_dir.iterdir() if p.is_dir() and p.name.startswith("model_v")],
        key=lambda p: p.stat().st_mtime,
    )
    if not dirs:
        die("model_missing", f"No model_v* directories under {models_dir}")
    return dirs[-1]


def main() -> None:
    print("=== PHASE 1: Resolve correct openclaw root ===")
    root = resolve_root()
    models_dir = root / "models"
    features_dir = root / "data" / "features"
    logs_dir = root / "pre_spike_symptom_pack" / "scanner" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    ok("ROOT_RESOLVED", root=str(root), models_dir=str(models_dir), features_dir=str(features_dir))

    print("=== PHASE 2: Locate model artifact ===")
    model_dir = latest_model(models_dir)
    model_file = model_dir / "lightgbm_model.txt"
    legacy_model = model_dir / "model.txt"
    feature_file = model_dir / "feature_names.json"
    manifest_file = model_dir / "manifest.json"

    if not model_file.exists() and legacy_model.exists():
        model_file.write_bytes(legacy_model.read_bytes())
        ok("CREATED_LIGHTGBM_MODEL", path=str(model_file))

    if not model_file.exists():
        die("model_file_missing", str(model_file))

    if not feature_file.exists():
        die("feature_names_missing", str(feature_file))

    features = json.loads(feature_file.read_text(encoding="utf-8"))
    if not isinstance(features, list) or not features:
        die("feature_names_invalid", str(feature_file))

    ok("MODEL_FOUND", model_version=model_dir.name, model_file=str(model_file), feature_count=len(features))

    print("=== PHASE 3: Validate model + manifest ===")
    sha = sha256_file(model_file)

    meta = {}
    metadata_file = model_dir / "metadata.json"
    if metadata_file.exists():
        try:
            meta = json.loads(metadata_file.read_text(encoding="utf-8"))
        except Exception:
            meta = {}

    threshold = float(meta.get("threshold_p95", meta.get("threshold", 0.5)))

    manifest = {
        "schema_version": 11,
        "scanner_contract": "direct_preflight_replay_v1",
        "model_version": model_dir.name,
        "model_type": "lightgbm",
        "model_file": "lightgbm_model.txt",
        "lightgbm_model_sha256": sha,
        "sha256": sha,
        "feature_names_file": "feature_names.json",
        "feature_names": features,
        "feature_count": len(features),
        "threshold": threshold,
        "threshold_tau": threshold,
        "threshold_p95": threshold,
    }
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    booster = lgb.Booster(model_file=str(model_file))
    ok("MODEL_LOAD_PASS", trees=booster.num_trees(), sha256=sha)

    print("=== PHASE 4: Validate feature files ===")
    files = sorted(features_dir.glob("*.parquet"))
    if not files:
        die("feature_files_missing", str(features_dir))

    frames = []
    skipped = []

    for p in files:
        df = pd.read_parquet(p)
        missing = [c for c in features if c not in df.columns]
        if missing:
            skipped.append({"file": p.name, "reason": "missing_model_columns", "missing_sample": missing[:5]})
            continue

        keep = [c for c in ["ts", "symbol", "label"] if c in df.columns] + features
        df = df[keep].replace([np.inf, -np.inf], np.nan).dropna(subset=features)

        if len(df):
            frames.append(df)

    if not frames:
        die("zero_usable_feature_rows", json.dumps(skipped[:10]))

    data = pd.concat(frames, ignore_index=True)

    if "ts" in data.columns:
        data["ts"] = pd.to_datetime(data["ts"], utc=True, errors="coerce")
        data = data.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)

    if len(data) > 50000:
        data = data.iloc[-50000:].copy().reset_index(drop=True)

    ok("FEATURES_VALID", usable_rows=len(data), skipped_files=skipped[:10])

    print("=== PHASE 5: Dry replay ===")
    x = data[features].astype("float32")
    pred = booster.predict(x)
    signals = pred >= threshold

    summary = {
        "event": "PREFLIGHT_REPLAY_PASS",
        "model_version": model_dir.name,
        "rows": int(len(data)),
        "signals": int(signals.sum()),
        "signal_rate": float(signals.mean()) if len(signals) else 0.0,
        "threshold": threshold,
        "feature_count": len(features),
        "trees": booster.num_trees(),
        "ts_iso": dt.datetime.now(dt.timezone.utc).isoformat(),
    }

    out = logs_dir / f"preflight_replay_{model_dir.name}_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(json.dumps(summary, indent=2))
    print(f"SUMMARY_PATH={out}")


if __name__ == "__main__":
    main()
