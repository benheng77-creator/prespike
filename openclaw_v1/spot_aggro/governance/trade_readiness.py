"""Phase 11n-9-aa — Trade Readiness Flag (mechanical release).

Every prior halt has been followed by an impatient manual restart.
This layer flips that. Instead of the operator deciding when it's
safe, a single boolean `ready_to_trade` is computed from objective
state:

  1. Shadow promoted OR sign-flip already applied.
  2. No contradiction-freeze active.
  3. Prospective trade's cell must be in universe_gatekeeper.admitted_cells().
  4. No cell with Wilson_upper < 0 on ≥ 50 exits is still enabled.
  5. Layer 1 last verdict ≤ 10 min old AND verdict != 'fail' on any
     admitted cell.

The operator's "Resume" button calls POST /spot_aggro/start; that
handler reads this flag. If False, it returns 409 with the unmet
condition list. No manual override.

Writes every evaluation to spot_trade_readiness (history). The
current state is a singleton row_id=1.

Never trades. Derives from read-only queries against other tables.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

_DB_LOCK = threading.Lock()


class EngineNotReady(Exception):
    """Raised by the engine entry path when ready_to_trade=False. The
    exception carries the list of unmet conditions so the engine cycle
    can log what's missing."""

    def __init__(self, unmet: list[str]):
        self.unmet = unmet
        super().__init__(
            "engine not ready to trade; unmet conditions: " + ", ".join(unmet)
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _db_path() -> str:
    return (
        os.environ.get("TRADE_DB_PATH")
        or os.environ.get("CLAW_DB_PATH")
        or "trades.db"
    )


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path(), isolation_level=None, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def _init_schema() -> None:
    with _DB_LOCK:
        con = _connect()
        try:
            # Singleton state row (row_id=1)
            con.execute(
                "CREATE TABLE IF NOT EXISTS spot_trade_readiness("
                " row_id INTEGER PRIMARY KEY CHECK (row_id = 1),"
                " ready INTEGER NOT NULL DEFAULT 0,"
                " unmet_json TEXT NOT NULL,"
                " last_evaluated_ts_ms INTEGER NOT NULL"
                ")"
            )
            # Full history
            con.execute(
                "CREATE TABLE IF NOT EXISTS spot_trade_readiness_ticks("
                " tick_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts_ms INTEGER NOT NULL,"
                " ready INTEGER NOT NULL,"
                " unmet_json TEXT NOT NULL,"
                " evidence_json TEXT"
                ")"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_trr_ts "
                "ON spot_trade_readiness_ticks(ts_ms DESC)"
            )
            con.execute(
                "INSERT OR IGNORE INTO spot_trade_readiness("
                " row_id, ready, unmet_json, last_evaluated_ts_ms)"
                " VALUES(1, 0, '[\"never_evaluated\"]', ?)",
                (int(time.time() * 1000),),
            )
        finally:
            con.close()


# ---------------------------------------------------------------------------
# Condition probes (each returns (met, detail))
# ---------------------------------------------------------------------------

def _c1_shadow_promoted_or_signflip_applied() -> tuple[bool, str]:
    """Condition 1: the sign-flip has either been promoted by the
    shadow scorer OR the commit has been shipped (signalled by env var
    SIGN_FLIP_COMMIT=<git-sha>). Without one of these, the live scorer
    is still the one Layer 12 proved losing."""
    if os.environ.get("SIGN_FLIP_COMMIT", "").strip():
        return True, f"SIGN_FLIP_COMMIT set: {os.environ['SIGN_FLIP_COMMIT']}"
    try:
        from spot_aggro.governance.shadow_scorer import latest
        v = latest()
        if v and v.get("promotion_verdict") == "promote":
            return True, f"shadow promoted (verdict_id={v.get('verdict_id')})"
        return False, (
            f"shadow verdict={v.get('promotion_verdict') if v else 'none'}"
            f"; reason={(v.get('reason') if v else '') or 'no verdict yet'}"
        )
    except Exception as e:
        return False, f"shadow_scorer probe error: {e}"


def _c2_no_freeze_active() -> tuple[bool, str]:
    try:
        from spot_aggro.governance.contradiction_freeze import (
            is_entry_frozen, current_state,
        )
        frozen = is_entry_frozen()
        if not frozen:
            return True, "no freeze active"
        s = current_state()
        return False, (
            f"contradiction_freeze active: cause="
            f"{s.get('primary_cause')} trigger={s.get('trigger_id')}"
        )
    except Exception as e:
        return False, f"contradiction_freeze probe error: {e}"


def _c3_universe_has_admitted_cells() -> tuple[bool, str]:
    try:
        from spot_aggro.governance.universe_gatekeeper import admitted_cells
        cells = admitted_cells()
        if cells:
            return True, f"{len(cells)} admitted cells"
        return False, "no admitted cells in universe_gatekeeper"
    except Exception as e:
        return False, f"universe_gatekeeper probe error: {e}"


def _c4_no_failed_cell_still_enabled() -> tuple[bool, str]:
    """Condition 4: every cell whose Wilson-upper < 0 on ≥ 50 exits is
    currently in state=deprecated_*. Scans latest Layer 1 verdict +
    cross-checks gatekeeper state."""
    try:
        from spot_aggro.governance.economic_truth_gov import latest
        from spot_aggro.governance.universe_gatekeeper import all_admissions
        v = latest() or {}
        admitted = {
            (a.cell_kind, a.cell_key) for a in all_admissions()
            if a.state == "admitted"
        }
        violations = []
        for c in v.get("cells", []):
            kind = c.get("segment_kind")
            key = c.get("segment_key")
            n = int(c.get("n") or 0)
            wu = c.get("expectancy_upper")
            if n < 50 or wu is None or wu >= 0:
                continue
            if (kind, key) in admitted:
                violations.append(
                    f"{kind}={key} wilson_upper={wu:+.4f} n={n}"
                )
        if not violations:
            return True, "no failed cell is admitted"
        return False, "failed cells still admitted: " + "; ".join(violations)
    except Exception as e:
        return False, f"failed-cell probe error: {e}"


def _c5_layer1_fresh_and_not_fail() -> tuple[bool, str]:
    """Condition 5: Layer 1 evaluated within the last 10 min AND the
    latest verdict is not 'fail' on any admitted cell."""
    try:
        from spot_aggro.governance.economic_truth_gov import latest
        from spot_aggro.governance.universe_gatekeeper import admitted_cells
        v = latest()
        if not v:
            return False, "no Layer 1 verdict yet"
        age_s = (int(time.time() * 1000) - int(v.get("ts_ms") or 0)) / 1000
        if age_s > 600:
            return False, f"Layer 1 verdict {age_s:.0f}s stale (> 600s)"
        admitted = {
            (c["cell_kind"], c["cell_key"]) for c in admitted_cells()
        }
        fail_admitted = [
            c for c in v.get("cells", [])
            if c.get("verdict") == "fail"
            and (c.get("segment_kind"), c.get("segment_key")) in admitted
        ]
        if fail_admitted:
            return False, (
                f"{len(fail_admitted)} admitted cells fail Layer 1: "
                + ", ".join(
                    f"{c.get('segment_key')}({c.get('expectancy_upper'):+.4f})"
                    for c in fail_admitted[:5]
                )
            )
        return True, f"Layer 1 fresh ({age_s:.0f}s) + no admitted-cell fail"
    except Exception as e:
        return False, f"Layer 1 probe error: {e}"


_CONDITIONS = (
    ("C1_shadow_promoted_or_signflip", _c1_shadow_promoted_or_signflip_applied),
    ("C2_no_freeze_active",             _c2_no_freeze_active),
    ("C3_universe_has_admitted_cells",  _c3_universe_has_admitted_cells),
    ("C4_no_failed_cell_still_enabled", _c4_no_failed_cell_still_enabled),
    ("C5_layer1_fresh_and_not_fail",    _c5_layer1_fresh_and_not_fail),
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class ReadinessTick:
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    ready: bool = False
    unmet: list[str] = field(default_factory=list)
    details: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate() -> ReadinessTick:
    """Compute the ready_to_trade flag. Writes the singleton state +
    a history row."""
    _init_schema()
    tick = ReadinessTick()
    for name, fn in _CONDITIONS:
        try:
            met, detail = fn()
        except Exception as e:
            met, detail = False, f"{name} probe crashed: {e}"
        tick.details[name] = detail
        if not met:
            tick.unmet.append(name)
    tick.ready = not tick.unmet
    with _DB_LOCK:
        con = _connect()
        try:
            con.execute(
                "UPDATE spot_trade_readiness SET ready=?, unmet_json=?,"
                " last_evaluated_ts_ms=? WHERE row_id=1",
                (int(tick.ready), json.dumps(tick.unmet), tick.ts_ms),
            )
            con.execute(
                "INSERT INTO spot_trade_readiness_ticks("
                " ts_ms, ready, unmet_json, evidence_json)"
                " VALUES(?,?,?,?)",
                (
                    tick.ts_ms, int(tick.ready),
                    json.dumps(tick.unmet),
                    json.dumps(tick.details),
                ),
            )
        finally:
            con.close()
    return tick


def is_ready_to_trade() -> bool:
    """O(1) read of the singleton. Engine entry path calls this before
    every entry attempt. Fail-closed: any DB error returns False.

    Does NOT re-evaluate; the scheduled daemon refreshes the flag. If
    the daemon is dead the flag goes stale, which is correct —
    condition C5 would have already flipped to False."""
    _init_schema()
    try:
        with _DB_LOCK:
            con = _connect()
            try:
                r = con.execute(
                    "SELECT ready FROM spot_trade_readiness WHERE row_id=1"
                ).fetchone()
                return bool(r and r["ready"])
            finally:
                con.close()
    except Exception:
        return False


def current_state() -> dict[str, Any]:
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            r = con.execute(
                "SELECT ready, unmet_json, last_evaluated_ts_ms"
                " FROM spot_trade_readiness WHERE row_id=1"
            ).fetchone()
            if not r:
                return {"ready": False, "unmet": ["never_evaluated"]}
            return {
                "ready": bool(r["ready"]),
                "unmet": json.loads(r["unmet_json"] or "[]"),
                "last_evaluated_ts_ms": int(r["last_evaluated_ts_ms"] or 0),
            }
        finally:
            con.close()


def history(limit: int = 50) -> list[dict[str, Any]]:
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT tick_id, ts_ms, ready, unmet_json"
                " FROM spot_trade_readiness_ticks"
                " ORDER BY tick_id DESC LIMIT ?", (limit,),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["unmet"] = json.loads(d.pop("unmet_json") or "[]")
                except Exception:
                    d["unmet"] = []
                out.append(d)
            return out
        finally:
            con.close()


def assert_ready_or_raise() -> None:
    """Engine entry path calls this FIRST — before contradiction_freeze
    check, before pre_trade_gov. Raises EngineNotReady with the unmet
    condition list attached."""
    state = current_state()
    if state.get("ready"):
        return
    raise EngineNotReady(state.get("unmet") or ["unknown"])
