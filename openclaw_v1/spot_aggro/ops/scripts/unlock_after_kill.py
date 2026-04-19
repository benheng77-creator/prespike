"""
unlock_after_kill.py — clear the APEX-Ω KILL_STATE.lock after operator review.

Requires operator to type the verbatim phrase "I HAVE REVIEWED THE LOGS AND UNLOCK"
AND enter a non-empty reason. Resets peak_equity to current account equity so
the next drawdown window starts fresh.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_PKG_ROOT = _HERE.parent.parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from shared.adapters.okx_unified import OKXUnified, OKXError
from spot_aggro.ops.persistence import state as persist
from spot_aggro.ops.risk import kill_switch


REQUIRED_PHRASE = "I HAVE REVIEWED THE LOGS AND UNLOCK"


def _get_live_equity_sync() -> float:
    """Best-effort equity read; returns 0 if adapter fails."""
    try:
        async def _r():
            a = OKXUnified()
            return await a.get_account_equity()
        return asyncio.run(_r())
    except Exception as exc:
        print(f"  (warning: live equity read failed: {exc})")
        return 0.0


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not kill_switch.is_locked():
        print("No KILL_STATE.lock present. Nothing to unlock.")
        return 0

    kill = persist.latest_unresolved_kill()
    if not kill:
        print("Lock file present but no unresolved kill event in DB.")
        print("Removing stale lock file.")
        kill_switch.clear_lock(kill_id=-1, operator="manual",
                               reason="stale lock, no DB record")
        return 0

    print("\n=== APEX-Ω KILL REVIEW ===")
    for k, v in kill.items():
        print(f"  {k}: {v}")
    print()
    print(f"Type exactly:  {REQUIRED_PHRASE}")
    entered = input("> ").strip()
    if entered != REQUIRED_PHRASE:
        print("Aborted. Lock remains.")
        return 2

    reason = input("Unlock reason (non-empty): ").strip()
    if not reason:
        print("Reason required. Lock remains.")
        return 2

    eq = _get_live_equity_sync()
    if eq > 0:
        persist.record_equity(eq, eq, 0)       # reset peak to current equity
        print(f"peak_equity reset to current equity ${eq:.2f}")
    else:
        print("(could not read equity; peak not reset)")

    kill_switch.clear_lock(kill_id=int(kill["id"]), operator="operator", reason=reason)
    print("Lock cleared. Run scripts/go_live.py to re-arm.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
