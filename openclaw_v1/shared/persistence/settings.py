"""
Operator-editable settings store.

Live knobs that the control panel can change WITHOUT restarting the engine:
  * universe_mode          "static" | "dynamic"
  * notification_filters   {event_type: bool}
  * notification_throttle  {event_type: min_interval_seconds}
  * module_enabled         {M1..M4: bool}
  * min_trades_per_day     int  (enforced by frequency governor)
  * max_trades_per_day     int

Settings are stored in a single-row SQLite table and read lazily by every
module. A version counter lets long-running loops detect changes.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from . import state as persist


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ops_settings (
    id              INTEGER PRIMARY KEY CHECK (id=1),
    version         INTEGER NOT NULL DEFAULT 1,
    updated_ts_ms   INTEGER NOT NULL,
    payload_json    TEXT NOT NULL
);
"""

_DEFAULTS: dict[str, Any] = {
    "universe_mode": "dynamic",
    # `notification_filters` gates what lands in the DB (and dashboard).
    # `notification_channels.{telegram,whatsapp}` gates outbound push
    # independently — consensus noise stays in the dashboard only.
    "notification_filters": {
        "engine.start": True, "engine.halt": True,
        "pair.enter": True, "pair.exit": True, "pair.reject": True,
        "consensus.fired": True, "consensus.vetoed": True,
        "kill.triggered": True, "kill.cleared": True,
        "pnl.report": True, "llm.health_degraded": True,
    },
    "notification_channels": {
        "telegram": {
            "engine.start": True, "engine.halt": True,
            "pair.enter": True, "pair.exit": True, "pair.reject": False,
            "consensus.fired": False, "consensus.vetoed": False,   # silenced on phone
            "kill.triggered": True, "kill.cleared": True,
            "pnl.report": True, "llm.health_degraded": True,
        },
        "whatsapp": {
            "engine.start": True, "engine.halt": True,
            "pair.enter": True, "pair.exit": True, "pair.reject": False,
            "consensus.fired": False, "consensus.vetoed": False,   # silenced on phone
            "kill.triggered": True, "kill.cleared": True,
            "pnl.report": True, "llm.health_degraded": True,
        },
    },
    "notification_throttle_s": {
        "pair.enter": 0, "pair.exit": 0, "pair.reject": 60,
        "consensus.fired": 120, "consensus.vetoed": 60,
        "kill.triggered": 0, "pnl.report": 0,
    },
    "module_enabled": {"M1_funding": True, "M2_statarb": True,
                       "M3_triangular": True, "M4_liqfade": True},
    "min_trades_per_day": 20,
    "max_trades_per_day": 60,
    "llm_verbose": True,            # log per-member reasoning into consensus_log
    "aggressive_mode": True,        # lower consensus_min to 0.50 if frequency falls behind
    "engine_paused": False,          # when True: ALL activity stops (no LLM, no orders, no scanning)
}


_lock = threading.Lock()
_initialized = False


def _init() -> None:
    global _initialized
    with _lock:
        if _initialized:
            return
        persist.init_schema()
        con = persist._connect()
        try:
            con.executescript(_SCHEMA)
            row = con.execute("SELECT COUNT(*) AS n FROM ops_settings WHERE id=1").fetchone()
            if not row["n"]:
                con.execute(
                    "INSERT INTO ops_settings (id, version, updated_ts_ms, payload_json) VALUES (1, 1, ?, ?)",
                    (int(time.time()*1000), json.dumps(_DEFAULTS)),
                )
            con.commit()
        finally:
            con.close()
        _initialized = True


def load() -> dict[str, Any]:
    _init()
    con = persist._connect()
    try:
        row = con.execute("SELECT payload_json, version, updated_ts_ms FROM ops_settings WHERE id=1").fetchone()
    finally:
        con.close()
    if not row:
        return dict(_DEFAULTS)
    try:
        payload = json.loads(row["payload_json"])
    except Exception:
        payload = {}
    # Merge defaults (forward-compat for new knobs added in code)
    merged = dict(_DEFAULTS)
    for k, v in payload.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    merged["_version"] = int(row["version"])
    merged["_updated_ts_ms"] = int(row["updated_ts_ms"])
    return merged


def save(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge patch into stored settings; bump version. Returns new full state."""
    _init()
    current = load()
    current.pop("_version", None)
    current.pop("_updated_ts_ms", None)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(current.get(k), dict):
            current[k] = {**current[k], **v}
        else:
            current[k] = v
    con = persist._connect()
    try:
        con.execute(
            "UPDATE ops_settings SET version=version+1, updated_ts_ms=?, payload_json=? WHERE id=1",
            (int(time.time()*1000), json.dumps(current)),
        )
        con.commit()
    finally:
        con.close()
    return load()


def notification_allowed(event_type: str) -> bool:
    """Whether the event should land in the DB (dashboard feed)."""
    s = load()
    return bool(s["notification_filters"].get(event_type, True))


def notification_channel_allowed(channel: str, event_type: str) -> bool:
    """Whether the event should be pushed to a specific channel (telegram / whatsapp)."""
    s = load()
    ch = s.get("notification_channels", {}).get(channel, {})
    return bool(ch.get(event_type, True))


def notification_throttle_s(event_type: str) -> int:
    s = load()
    return int(s["notification_throttle_s"].get(event_type, 60))


def module_enabled(module_name: str) -> bool:
    s = load()
    return bool(s["module_enabled"].get(module_name, True))
