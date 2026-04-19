"""
Preset + run-history persistence — JSON files under runtime/backtest/.

Presets are user-configurable scenario stacks the operator wants to
re-run quickly. Runs are immutable result snapshots used for compare
mode and audit replay.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent / "runtime" / "backtest"
PRESETS_DIR = ROOT / "presets"
RUNS_DIR = ROOT / "runs"


# Built-in presets — always available, never on disk. Users get a populated
# dropdown on first run. Names prefixed with "★" are built-ins.
BUILTIN_PRESETS: dict[str, dict] = {
    "Starter-Safe": {
        "description": "Gentle test. Balanced strategy · calm market · $10k · 30 days.",
        "payload": {
            "capital": 10_000, "days": 30, "mode": "single", "seed": 42,
            "sec_per_bar": 3600, "start_price": 50_000, "use_gemini": False,
            "strategy": "balanced",
            "cost": {"fee_bps_per_side": 4.0, "leverage": 1.0},
            "overlays": {},
            "picks": [{"code": "SIDEWAYS_CHOP", "weight": 1.0, "repeat": 1}],
            "label": "Starter-Safe",
        },
    },
    "Bull-Run-2021": {
        "description": "2021 bull euphoria replay. Balanced · $100k · 180 days.",
        "payload": {
            "capital": 100_000, "days": 180, "mode": "single", "seed": 42,
            "sec_per_bar": 900, "start_price": 30_000, "use_gemini": False,
            "strategy": "balanced",
            "cost": {"fee_bps_per_side": 4.0, "leverage": 1.0},
            "overlays": {},
            "picks": [{"code": "S1_2021_BULL", "weight": 1.0, "repeat": 1}],
            "label": "Bull-Run-2021",
        },
    },
    "Bear-Crash-2022": {
        "description": "Luna/FTX collapse replay. Balanced · $100k · 180 days.",
        "payload": {
            "capital": 100_000, "days": 180, "mode": "single", "seed": 42,
            "sec_per_bar": 900, "start_price": 48_000, "use_gemini": False,
            "strategy": "balanced",
            "cost": {"fee_bps_per_side": 4.0, "leverage": 1.0},
            "overlays": {},
            "picks": [{"code": "S2_2022_BEAR", "weight": 1.0, "repeat": 1}],
            "label": "Bear-Crash-2022",
        },
    },
    "Full-Cycle-3yr": {
        "description": "Bull → bear → recovery → halving. Balanced · $100k · 1095 days.",
        "payload": {
            "capital": 100_000, "days": 1095, "mode": "chained", "seed": 42,
            "sec_per_bar": 14400, "start_price": 30_000, "use_gemini": False,
            "strategy": "balanced",
            "cost": {"fee_bps_per_side": 4.0, "leverage": 1.0},
            "overlays": {},
            "picks": [
                {"code": "S1_2021_BULL", "weight": 1.0, "repeat": 1},
                {"code": "S2_2022_BEAR", "weight": 1.0, "repeat": 1},
                {"code": "S3_2023_RECOVERY", "weight": 1.0, "repeat": 1},
                {"code": "S4_2024_HALVING_ETF", "weight": 1.0, "repeat": 1},
            ],
            "label": "Full-Cycle-3yr",
        },
    },
    "Stress-Test": {
        "description": "Black swan + bear + chop with harsh overlays. Survival test.",
        "payload": {
            "capital": 100_000, "days": 90, "mode": "chained", "seed": 42,
            "sec_per_bar": 900, "start_price": 50_000, "use_gemini": False,
            "strategy": "balanced",
            "cost": {"fee_bps_per_side": 7.0, "leverage": 1.0},
            "overlays": {
                "slippage_mult": 2.0, "spread_mult": 2.0,
                "no_fill_prob": 0.05, "partial_fill_prob": 0.1,
                "liquidity_vacuum_prob": 0.005, "latency_bars": 2,
                "funding_drag_bps_8h": 10.0, "exec_stress_prob": 0.1,
                "black_swan_inject": True, "black_swan_drop": -0.30,
            },
            "picks": [
                {"code": "BLACK_SWAN", "weight": 1.0, "repeat": 1},
                {"code": "S2_2022_BEAR", "weight": 1.0, "repeat": 1},
                {"code": "SIDEWAYS_CHOP", "weight": 1.0, "repeat": 1},
            ],
            "label": "Stress-Test",
        },
    },
    "Aggressive-Momentum": {
        "description": "Aggressive strategy on trending markets. Best-case upside.",
        "payload": {
            "capital": 100_000, "days": 180, "mode": "chained", "seed": 42,
            "sec_per_bar": 900, "start_price": 50_000, "use_gemini": False,
            "strategy": "aggressive",
            "cost": {"fee_bps_per_side": 4.0, "leverage": 2.0},
            "overlays": {},
            "picks": [
                {"code": "S1_2021_BULL", "weight": 1.0, "repeat": 1},
                {"code": "S4_2024_HALVING_ETF", "weight": 1.0, "repeat": 1},
            ],
            "label": "Aggressive-Momentum",
        },
    },
}


def _ensure() -> None:
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


def save_preset(name: str, payload: dict) -> Path:
    _ensure()
    safe = "".join(c for c in name if c.isalnum() or c in ("-", "_")).strip("_")
    if not safe:
        raise ValueError("preset name empty after sanitisation")
    path = PRESETS_DIR / f"{safe}.json"
    body = {
        "name": safe,
        "saved_at": int(time.time()),
        "payload": payload,
    }
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return path


def load_preset(name: str) -> dict:
    # Built-ins first — always available, never missing.
    if name in BUILTIN_PRESETS:
        b = BUILTIN_PRESETS[name]
        return {
            "name": name,
            "builtin": True,
            "description": b.get("description", ""),
            "payload": b["payload"],
        }
    safe = "".join(c for c in name if c.isalnum() or c in ("-", "_")).strip("_")
    path = PRESETS_DIR / f"{safe}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def list_presets() -> list[dict]:
    _ensure()
    out = []
    # Built-ins come first so the dropdown is never empty.
    for name, body in BUILTIN_PRESETS.items():
        out.append({
            "name": name,
            "builtin": True,
            "description": body.get("description", ""),
            "saved_at": None,
        })
    for p in sorted(PRESETS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            name = data.get("name") or p.stem
            if name in BUILTIN_PRESETS:
                continue  # don't double-list if user saved over a builtin
            out.append({
                "name": name,
                "builtin": False,
                "description": data.get("description", ""),
                "saved_at": data.get("saved_at"),
            })
        except Exception:
            continue
    return out


def save_run(result: Any) -> dict:
    _ensure()
    if hasattr(result, "__dict__"):
        body = _to_jsonable(result)
    elif isinstance(result, dict):
        body = result
    else:
        raise TypeError("result must be RunResult or dict")
    h = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:12]
    rid = f"{int(time.time())}_{h}"
    path = RUNS_DIR / f"{rid}.json"
    path.write_text(json.dumps({"id": rid, **body}, indent=2, default=str), encoding="utf-8")
    return {"id": rid, "path": str(path)}


def load_run(run_id: str) -> dict:
    path = RUNS_DIR / f"{run_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def list_runs(limit: int = 50) -> list[dict]:
    _ensure()
    out = []
    files = sorted(RUNS_DIR.glob("*.json"), reverse=True)[:limit]
    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out.append({
                "id": data.get("id") or p.stem,
                "label": data.get("config", {}).get("label", ""),
                "summary": data.get("summary", {}),
                "started_ts": data.get("started_ts"),
            })
        except Exception:
            continue
    return out


def _to_jsonable(obj: Any) -> Any:
    from dataclasses import is_dataclass
    if is_dataclass(obj):
        return {k: _to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj
