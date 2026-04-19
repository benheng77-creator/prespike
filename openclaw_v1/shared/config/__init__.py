"""Config loader.

Phase 11n-9-t — accepts only 'ops' (shared-ops config) and 'spot_aggro'.
Every legacy engine key was purged; passing an unknown engine raises
ValueError.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import yaml


_CONFIG_DIR = Path(__file__).resolve().parent
_lock = threading.Lock()
_cache: dict[str, dict[str, Any]] = {}


def _inject_env(cfg: dict[str, Any]) -> dict[str, Any]:
    cfg["_env"] = {
        "okx_hostname": os.environ.get(
            "OKX_HOSTNAME",
            cfg.get("okx", {}).get("default_hostname", "www.okx.com"),
        ).strip(),
        "okx_api_key": os.environ.get("OKX_API_KEY", "").strip(),
        "okx_api_secret": os.environ.get("OKX_API_SECRET", "").strip(),
        "okx_passphrase": os.environ.get("OKX_PASSPHRASE", "").strip(),
    }
    return cfg


def load(engine: str = "ops", force_reload: bool = False) -> dict[str, Any]:
    """Load engine-specific config. Accepts 'ops' or 'spot_aggro'."""
    _filemap = {
        "ops": "ops_config.yml",
        "spot_aggro": "spot_aggro_config.yml",
    }
    if engine not in _filemap:
        raise ValueError(
            f"unknown config engine {engine!r}; accepted: {sorted(_filemap)}"
        )
    filename = _filemap[engine]
    with _lock:
        if engine not in _cache or force_reload:
            path = _CONFIG_DIR / filename
            with open(path, "r", encoding="utf-8") as f:
                _cache[engine] = _inject_env(yaml.safe_load(f))
        return _cache[engine]


def universe_symbols() -> list[str]:
    cfg = load()
    return [u["symbol"] for u in cfg["universe"]]


def coin_meta(symbol: str) -> dict[str, Any] | None:
    cfg = load()
    for u in cfg["universe"]:
        if u["symbol"] == symbol:
            return u
    return None


def statarb_pairs() -> list[dict[str, Any]]:
    return load().get("statarb_pairs", [])
