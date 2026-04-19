"""
APEX-Ω kill switch. 5% equity drawdown → emergency close all → lock file written.
Restart requires running scripts/unlock_after_kill.py with operator confirmation.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

from ..config import load as load_cfg
from ..persistence import state as persist

try:
    from ..notifications import router as _notify
except Exception:
    _notify = None          # notifications package optional at import time


log = logging.getLogger("ops.risk.kill_switch")


def _lock_path() -> Path:
    cfg = load_cfg()
    p = Path(cfg["persistence"]["kill_lock_path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def is_locked() -> bool:
    return _lock_path().exists()


def write_lock(*, reason: str, drawdown_pct: float,
               equity_usd: float, peak_usd: float) -> int:
    """Write KILL_STATE.lock + log to DB. Returns kill_event id."""
    path = _lock_path()
    ts = int(time.time())
    path.write_text(
        f"KILL at {ts}\n"
        f"reason: {reason}\n"
        f"drawdown_pct: {drawdown_pct:.6f}\n"
        f"equity_usd: {equity_usd:.4f}\n"
        f"peak_usd: {peak_usd:.4f}\n",
        encoding="utf-8",
    )
    kill_id = persist.record_kill_event(
        reason=reason, drawdown_pct=drawdown_pct,
        equity_usd=equity_usd, peak_usd=peak_usd,
    )
    log.critical("APEX kill fired: dd=%.4f eq=$%.2f peak=$%.2f → %s",
                 drawdown_pct, equity_usd, peak_usd, path)
    if _notify is not None:
        try:
            _notify.kill_triggered(reason=reason, drawdown_pct=drawdown_pct,
                                   equity_usd=equity_usd, peak_usd=peak_usd)
        except Exception:
            log.exception("kill notify failed")
    return kill_id


def clear_lock(*, kill_id: int, operator: str, reason: str) -> None:
    path = _lock_path()
    if path.exists():
        path.unlink()
    persist.record_kill_unlock(kill_id=kill_id, by=operator, reason=reason)
    log.warning("APEX kill cleared: operator=%s reason=%r", operator, reason)
    if _notify is not None:
        try:
            _notify.kill_cleared(operator=operator, reason=reason)
        except Exception:
            log.exception("kill-clear notify failed")


def check_and_trigger(*, equity_usd: float, peak_usd: float) -> Optional[int]:
    """Return kill_event_id if drawdown crossed threshold, else None."""
    cfg = load_cfg()
    threshold = float(cfg["risk"]["kill_drawdown_pct"])
    if peak_usd <= 0:
        return None
    dd = (peak_usd - equity_usd) / peak_usd
    if dd > threshold and not is_locked():
        return write_lock(
            reason=f"drawdown {dd:.2%} > threshold {threshold:.2%}",
            drawdown_pct=dd, equity_usd=equity_usd, peak_usd=peak_usd,
        )
    return None
