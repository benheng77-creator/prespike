"""
SPOT AGGRO — Independent engine package.

Self-owned package with no apex_omega dependencies. Uses ``shared/`` for
cross-engine infrastructure (adapters, llm, persistence, notifications)
and ``spot_aggro.api`` for its own HTTP surface under ``/spot_aggro/*``.

Own config: spot_aggro_config.yml
Own formulas, thresholds, scoring, tiers, swarm.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

# Phase 11g — primary engine class is now ``SpotAggroEngine``. The legacy
# name ``APEX_Spot_Aggro`` is retained as a backward-compat alias inside
# ``.engine`` for rolling migration; new code in this package imports
# ``SpotAggroEngine`` directly.
from .engine import SpotAggroEngine


log = logging.getLogger("spot_aggro")

_engine_instance: Optional[SpotAggroEngine] = None
_thread: Optional[threading.Thread] = None


def start_engine(*, dry_run: bool = False) -> None:
    global _engine_instance, _thread
    if _thread and _thread.is_alive():
        log.info("spot_aggro already running")
        return
    _engine_instance = SpotAggroEngine(dry_run=dry_run)

    def _run():
        asyncio.run(_engine_instance.run_forever())

    _thread = threading.Thread(target=_run, name="spot_aggro_engine", daemon=True)
    _thread.start()
    log.info("spot_aggro engine started (dry_run=%s)", dry_run)


def stop_engine() -> None:
    global _engine_instance
    if _engine_instance:
        _engine_instance._stop.set()
        log.info("spot_aggro engine stop requested")
