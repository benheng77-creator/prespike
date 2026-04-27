from __future__ import annotations

import argparse, datetime as dt, hashlib, json, time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "ops" / "pre_spike_24x7"
DATA = BASE / "data" / "binance_1m"
LOGS = BASE / "logs"
STATE = BASE / "state"
MODELS = ROOT / "models"

INTERVAL_SECONDS = 30
MIN_BARS = 80

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

def write_jsonl(name: str, event: dict) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    event["ts_iso"] = now_iso()
    p = LOGS / f"{name}_{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')}.jsonl"
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, separators=(",", ":"), default=str) + "\n")

def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()

def latest_model() -> Path:
    dirs = sorted([p for p in MODELS.glob("model_v*") if p.is_dir()], key=lambda p: p.stat().st_mtime)
    if not dirs:
        raise RuntimeError(f"no model_v* under {MODELS}")
    return dirs[-1]

def load_model():
    m = latest_model()
    model_file = m / "lightgbm_model.txt"
    legacy = m / "model.txt"
    if not model_file.exists() and legacy.exists():
        model_file.write_bytes(legacy.read_bytes())
    if not model_file.exists():
        raise RuntimeError(f"missing model: {model_file}")
    features = json.loads((m / "feature_names.json").read_text(encoding="utf-8"))
    manifest = {}
    if (m / "manifest.json").exists():
        manifest = json.loads((m / "manifest.json").read_text(encoding="utf-8"))
    expected = manifest.get("lightgbm_model_sha256") or manifest.get("sha256")
    if expected and expected != sha256_file(model_file):
        raise RuntimeError("model sha256 mismatch")
    threshold = float(manifest.get("threshold_tau") or manifest.get("threshold") or manifest.get("threshold_p95") or 0.5)
    booster = lgb.Booster(model_file=str(model_file))
    return m, booster, features, threshold

def read_symbol(symbol: str, limit: int = 500) -> pd.DataFrame:
    p = DATA / f"{symbol}.jsonl"
    if not p.exists():
        return pd.DataFrame()
    with open(p, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        block = min(size, 2_000_000)
        f.seek(-block, 2)
        lines = f.read().decode("utf-8", errors="ignore").splitlines()
    rows = []
    for line in lines[-limit:]:
        if line.strip():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).drop_duplicates(subset=["ts_ms"]).sort_values("ts_ms").tail(limit).reset_index(drop=True)

def build_features(df: pd.DataFrame, symbol: str) -> dict | None:
    if len(df) < MIN_BARS:
        return None
    close = pd.to_numeric(df["close"], errors="coerce")
    open_ = pd.to_numeric(df["open"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    ret1 = close.pct_change(1)
    feat = pd.DataFrame({
        "ret_1": ret1,
        "ret_3": close.pct_change(3),
        "ret_6": close.pct_change(6),
        "ret_12": close.pct_change(12),
        "ret_24": close.pct_change(24),
        "range_pct": (high - low) / close.replace(0, np.nan),
        "body_pct": (close - open_) / open_.replace(0, np.nan),
        "upper_wick_pct": (high - np.maximum(open_, close)) / close.replace(0, np.nan),
        "lower_wick_pct": (np.minimum(open_, close) - low) / close.replace(0, np.nan),
        "vol_chg_3": vol / vol.rolling(3, min_periods=3).mean().replace(0, np.nan),
        "vol_chg_12": vol / vol.rolling(12, min_periods=12).mean().replace(0, np.nan),
        "vol_chg_24": vol / vol.rolling(24, min_periods=24).mean().replace(0, np.nan),
        "rv_6": ret1.rolling(6, min_periods=6).std(),
        "rv_12": ret1.rolling(12, min_periods=12).std(),
        "rv_24": ret1.rolling(24, min_periods=24).std(),
        "rv_48": ret1.rolling(48, min_periods=48).std(),
        "mom_6": close / close.shift(6) - 1.0,
        "mom_12": close / close.shift(12) - 1.0,
        "mom_24": close / close.shift(24) - 1.0,
        "mom_48": close / close.shift(48) - 1.0,
        "dist_sma_12": close / close.rolling(12, min_periods=12).mean() - 1.0,
        "dist_sma_24": close / close.rolling(24, min_periods=24).mean() - 1.0,
        "dist_sma_48": close / close.rolling(48, min_periods=48).mean() - 1.0,
        "high_break_24": close / high.rolling(24, min_periods=24).max().shift(1) - 1.0,
        "low_reclaim_24": close / low.rolling(24, min_periods=24).min().shift(1) - 1.0,
    }).replace([np.inf, -np.inf], np.nan)
    last = feat.iloc[-1]
    if last.isna().any():
        return None
    out = last.to_dict()
    out["symbol"] = symbol
    out["ts_ms"] = int(df["ts_ms"].iloc[-1])
    out["close"] = float(df["close"].iloc[-1])
    return out

def cycle() -> None:
    m, booster, feature_names, threshold = load_model()
    observations = 0
    signals = 0
    errors = []
    for p in sorted(DATA.glob("*.jsonl")):
        sym = p.stem
        try:
            df = read_symbol(sym)
            feat = build_features(df, sym)
            if feat is None:
                continue
            missing = [c for c in feature_names if c not in feat]
            if missing:
                errors.append({"symbol": sym, "error": f"missing_features:{missing[:5]}"})
                continue
            x = pd.DataFrame([{c: feat[c] for c in feature_names}]).astype("float32")
            p_spike = float(booster.predict(x)[0])
            sig = p_spike >= threshold
            event = {
                "event": "scanner_observation",
                "model_version": m.name,
                "symbol": sym,
                "bar": "1m",
                "ts_ms": feat["ts_ms"],
                "close": feat["close"],
                "p_spike": p_spike,
                "threshold": threshold,
                "signal": bool(sig),
            }
            write_jsonl("scanner_observations", event)
            observations += 1
            if sig:
                write_jsonl("scanner_signals", event)
                signals += 1
        except Exception as e:
            errors.append({"symbol": sym, "error": f"{type(e).__name__}: {e}"})
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "scanner_heartbeat.json").write_text(json.dumps({
        "ts_iso": now_iso(),
        "observations": observations,
        "signals": signals,
        "errors": errors[:10],
    }, indent=2), encoding="utf-8")
    write_jsonl("scanner_health", {"event": "scanner_cycle", "observations": observations, "signals": signals, "error_count": len(errors), "errors": errors[:10]})

def preflight() -> None:
    m, booster, features, threshold = load_model()
    LOGS.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)
    write_jsonl("scanner_health", {"event": "scanner_preflight_pass", "model_version": m.name, "features": len(features), "threshold": threshold, "trees": booster.num_trees()})
    print("SCANNER_PREFLIGHT_PASS")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    preflight()
    if args.preflight:
        return
    if args.once:
        cycle()
        print("SCANNER_ONCE_PASS")
        return
    write_jsonl("scanner_health", {"event": "scanner_start", "interval_seconds": INTERVAL_SECONDS})
    while True:
        try:
            cycle()
        except Exception as e:
            write_jsonl("scanner_health", {"event": "scanner_uncaught_error", "error": f"{type(e).__name__}: {e}"})
        time.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
