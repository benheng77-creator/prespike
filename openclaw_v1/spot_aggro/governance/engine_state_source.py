"""Phase 11n-9-dd — Canonical engine-state source.

One authoritative function for "what state is the engine in?" so that
card_truth_gov, the dashboard, and the heartbeat writer all agree.

States
------
- running              : engine loop alive and cycling
- stopped_by_operator  : operator hit Stop; no crash, no kill
- halted_by_kill       : kill-switch latched (drawdown breach, etc.)
- crashed              : engine instance exists but loop died unexpectedly
- idle                 : server just booted; engine never started this
                        process. Treated as stopped_by_operator for
                        freshness/card-truth purposes.

Why a separate module?
- card_truth_gov needs to distinguish "stale because engine crashed"
  (real FAIL) from "stale because operator halted" (expected IDLE).
- Heartbeat writer needs to keep `equity_marks` fresh in both idle and
  stopped-by-operator states so cards don't flip to STALE after 10 min.
- Dashboard needs a single pill: OK / IDLE / HALTED / CRASHED / FAIL.

No writes. No imports from engine.py at module scope (avoids circular).
"""
from __future__ import annotations

import time
from typing import Any, Literal

EngineState = Literal[
    "running", "stopped_by_operator", "halted_by_kill", "crashed", "idle"
]


def current_engine_state() -> dict[str, Any]:
    """Return the canonical engine state + diagnostic context.

    Shape:
      {
        "state": "running" | "stopped_by_operator" | "halted_by_kill"
                 | "crashed" | "idle",
        "is_intentional_stop": bool,   # True if idle/stopped/halted
        "reason": str,                 # short human-readable
        "last_cycle_ts_ms": int|None,  # when the loop last ticked
        "ts_ms": int,                  # when this verdict was computed
      }

    Fail-open: on any exception returns idle with the error captured.
    """
    now_ms = int(time.time() * 1000)
    try:
        from spot_aggro import _engine_instance
    except Exception as e:
        return {
            "state": "idle",
            "is_intentional_stop": True,
            "reason": f"engine module import failed: {str(e)[:80]}",
            "last_cycle_ts_ms": None,
            "ts_ms": now_ms,
        }

    if _engine_instance is None:
        return {
            "state": "idle",
            "is_intentional_stop": True,
            "reason": "engine never started this process",
            "last_cycle_ts_ms": None,
            "ts_ms": now_ms,
        }

    # Inspect the engine instance.
    try:
        st = _engine_instance.status() or {}
    except Exception as e:
        return {
            "state": "crashed",
            "is_intentional_stop": False,
            "reason": f"status() raised: {str(e)[:80]}",
            "last_cycle_ts_ms": None,
            "ts_ms": now_ms,
        }

    running = bool(st.get("running", False))
    halted = bool(st.get("halted", False))
    mode = str(st.get("mode", ""))
    last_cycle_ts_ms = st.get("last_cycle_ts_ms")
    if isinstance(last_cycle_ts_ms, (int, float)):
        last_cycle_ts_ms = int(last_cycle_ts_ms)
    else:
        last_cycle_ts_ms = None

    if halted:
        return {
            "state": "halted_by_kill",
            "is_intentional_stop": True,
            "reason": "kill-switch latched",
            "last_cycle_ts_ms": last_cycle_ts_ms,
            "ts_ms": now_ms,
        }
    if running:
        # If the engine says it's running but the last cycle is > 10
        # minutes old, treat that as crashed (the loop is not ticking).
        if last_cycle_ts_ms is not None and (now_ms - last_cycle_ts_ms) > 600_000:
            return {
                "state": "crashed",
                "is_intentional_stop": False,
                "reason": f"no cycle in {(now_ms - last_cycle_ts_ms) // 1000}s",
                "last_cycle_ts_ms": last_cycle_ts_ms,
                "ts_ms": now_ms,
            }
        return {
            "state": "running",
            "is_intentional_stop": False,
            "reason": f"mode={mode or 'active'}",
            "last_cycle_ts_ms": last_cycle_ts_ms,
            "ts_ms": now_ms,
        }

    # Engine instance exists but running=False → operator stopped it.
    return {
        "state": "stopped_by_operator",
        "is_intentional_stop": True,
        "reason": "engine.running=False (operator stop)",
        "last_cycle_ts_ms": last_cycle_ts_ms,
        "ts_ms": now_ms,
    }


def is_intentionally_stopped() -> bool:
    """True when the engine is NOT expected to be producing fresh data.

    Used by card_truth_gov freshness rules and the dashboard pill logic
    to suppress STALE/FAIL on cards that depend on engine activity when
    the operator has chosen to keep it off.
    """
    try:
        return bool(current_engine_state().get("is_intentional_stop"))
    except Exception:
        # Fail-open to "intentional stop" so a broken state-source can
        # never mask a real engine crash as a fake FAIL card.
        return True
