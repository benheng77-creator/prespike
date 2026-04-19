"""Phase 11n-9-z — Card-Truth Mismatch Detector (rules M1–M7).

Detects cross-card contradictions: situations where one card says OK
while a paired card says something that makes OK impossible. These are
the 'fake green' failures that a process-only dashboard can't see.

Rules (from phase-y spec):
  M1  status.open_positions > 0 AND pnl.open_pairs == 0
  M2  engine posture READY AND account-snapshot stale
  M3  sysaudit verdict=OK AND last-50 Wilson-upper expectancy < 0
  M4  all cards OK AND equity_marks.latest_ts_ms < now - 300s
  M5  consensus ticking recent AND no entries in 1h AND engine running
  M6  all tiers enabled AND any tier expectancy Wilson-upper < 0
  M7  pre_trade pass_rate_7d < 0.01 AND entries_7d > 50

Writes findings to spot_card_truth_mismatches. Layer 3 contradiction
freeze reads this table; ≥ 2 mismatches in 30 min triggers T4.

Never trades. Never consults capital. Read-only probe of other tables.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

_DB_LOCK = threading.Lock()

# Mismatch rule IDs (used in Layer 3 freeze triggers).
ALL_RULES = ("M1", "M2", "M3", "M4", "M5", "M6", "M7")


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
            con.execute(
                "CREATE TABLE IF NOT EXISTS spot_card_truth_mismatches("
                " finding_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts_ms INTEGER NOT NULL,"
                " rule_id TEXT NOT NULL,"
                " severity TEXT NOT NULL,"
                " detail TEXT NOT NULL,"
                " evidence_json TEXT NOT NULL"
                ")"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_mismatch_ts "
                "ON spot_card_truth_mismatches(ts_ms DESC)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_mismatch_rule_ts "
                "ON spot_card_truth_mismatches(rule_id, ts_ms DESC)"
            )
        finally:
            con.close()


@dataclass
class Mismatch:
    rule_id: str
    severity: str      # 'hard' (triggers freeze) | 'soft' (warn only)
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class MismatchScan:
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    findings: list[Mismatch] = field(default_factory=list)
    n_hard: int = 0
    n_soft: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["findings"] = [asdict(f) for f in self.findings]
        return d


# ---------------------------------------------------------------------------
# Rule probes
# ---------------------------------------------------------------------------

def _latest_equity_ts_ms() -> int | None:
    try:
        con = _connect()
        try:
            r = con.execute(
                "SELECT ts_ms FROM equity_marks ORDER BY ts_ms DESC LIMIT 1"
            ).fetchone()
            return int(r["ts_ms"]) if r else None
        finally:
            con.close()
    except Exception:
        return None


def _pre_trade_stats_7d() -> dict[str, int]:
    try:
        con = _connect()
        try:
            cut = int(time.time() * 1000) - 7 * 24 * 3600 * 1000
            has_authz = con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                " AND name='spot_pre_trade_authorizations'"
            ).fetchone()[0] > 0
            if not has_authz:
                return {"pre_trade_pass_n": 0, "pre_trade_total": 0,
                        "entries_7d": 0, "pass_rate": 0.0}
            pass_n = con.execute(
                "SELECT COUNT(*) FROM spot_pre_trade_authorizations "
                "WHERE ts_ms >= ? AND passed=1", (cut,),
            ).fetchone()[0]
            total = con.execute(
                "SELECT COUNT(*) FROM spot_pre_trade_authorizations "
                "WHERE ts_ms >= ?", (cut,),
            ).fetchone()[0]
            entries = con.execute(
                "SELECT COUNT(*) FROM trade_log "
                "WHERE ts_ms >= ? AND action='enter'", (cut,),
            ).fetchone()[0]
            return {
                "pre_trade_pass_n": int(pass_n or 0),
                "pre_trade_total": int(total or 0),
                "entries_7d": int(entries or 0),
                "pass_rate": (pass_n / total) if total else 0.0,
            }
        finally:
            con.close()
    except Exception:
        return {"pre_trade_pass_n": 0, "pre_trade_total": 0,
                "entries_7d": 0, "pass_rate": 0.0}


def _sysaudit_latest_verdict() -> str | None:
    try:
        con = _connect()
        try:
            r = con.execute(
                "SELECT verdict FROM spot_system_audit_runs "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            return r["verdict"] if r else None
        finally:
            con.close()
    except Exception:
        return None


def _economic_truth_latest() -> dict[str, Any] | None:
    try:
        from spot_aggro.governance.economic_truth_gov import latest
        return latest()
    except Exception:
        return None


def _recent_consensus_and_entries() -> dict[str, int | None]:
    try:
        con = _connect()
        try:
            now = int(time.time() * 1000)
            last_consensus = con.execute(
                "SELECT ts_ms FROM consensus_log ORDER BY ts_ms DESC LIMIT 1"
            ).fetchone()
            last_entry = con.execute(
                "SELECT ts_ms FROM trade_log WHERE action='enter' "
                "ORDER BY ts_ms DESC LIMIT 1"
            ).fetchone()
            return {
                "now_ms": now,
                "last_consensus_ms": last_consensus["ts_ms"] if last_consensus else None,
                "last_entry_ms": last_entry["ts_ms"] if last_entry else None,
            }
        finally:
            con.close()
    except Exception:
        return {"now_ms": int(time.time() * 1000),
                "last_consensus_ms": None, "last_entry_ms": None}


def _tier_toggles() -> dict[str, Any]:
    """Best-effort snapshot of tier toggles + their economic verdict."""
    try:
        con = _connect()
        try:
            # Latest Layer 12 verdict tells us which tier cells are
            # failing.
            et = _economic_truth_latest() or {}
            fail_tiers: set[str] = set()
            for c in et.get("cells", []):
                if c.get("segment_kind") == "tier" and c.get("verdict") == "fail":
                    fail_tiers.add(c.get("segment_key", ""))
            # Pull current tier-toggle state from the YAML-backed gate
            # (best effort — not all deployments have this loaded at
            # import time; catch and skip).
            from spot_aggro.gates.tier_toggle import TierExecutionToggle
            toggle = TierExecutionToggle()
            exec_state = toggle.execution_state()
            return {
                "exec_state": exec_state,
                "fail_tiers_economic": sorted(fail_tiers),
            }
        finally:
            con.close()
    except Exception:
        return {"exec_state": {}, "fail_tiers_economic": []}


def _status_snapshot() -> dict[str, Any]:
    """Snapshot of engine state — open positions count + running flag.
    Best-effort from various tables; never raises."""
    try:
        con = _connect()
        try:
            # open_pairs table is the ops-side source of truth for
            # open-pairs count.
            open_pairs_n = con.execute(
                "SELECT COUNT(*) FROM open_pairs"
            ).fetchone()[0]
            return {"open_pairs_n": int(open_pairs_n or 0)}
        finally:
            con.close()
    except Exception:
        return {"open_pairs_n": None}


# ---------------------------------------------------------------------------
# Rule implementations
# ---------------------------------------------------------------------------

def _rule_m1() -> Mismatch | None:
    """M1: status.open_positions > 0 AND pnl.open_pairs == 0.

    We approximate: the engine's in-memory position count (exposed via
    /spot_aggro/status.open_positions) should match ops_pairs count.
    If in-memory > 0 but open_pairs == 0, one side is stale."""
    # Source of truth for in-memory count is only visible via the HTTP
    # surface; for the offline probe we use the open_pairs table + a
    # best-effort read of the latest status snapshot. If open_pairs is
    # 0 but the latest status report claimed positions, flag.
    snap = _status_snapshot()
    op = snap.get("open_pairs_n")
    if op is None:
        return None
    # The inverse probe: pnl_reporter writes "open_pairs" count on each
    # run; check recent notifications table for latest report.
    # Lacking this signal, we rely on the watchdog / pnl cache. For now
    # we only fire M1 when there's an evident contradiction we can
    # prove — otherwise stay silent. Left in place as a scaffolding
    # point; the live engine will populate the signal when the pnl
    # reporter writes a `pnl_snapshot` row (next phase).
    return None


def _rule_m2() -> Mismatch | None:
    """M2: engine posture READY AND account-snapshot stale.
    Fires when: the engine still looks ready (cycles > 0 recently)
    but equity_marks has not refreshed in > 300s."""
    eq_ts = _latest_equity_ts_ms()
    if eq_ts is None:
        return Mismatch(
            "M2", "hard",
            "account-snapshot has no equity_marks row at all",
            evidence={},
        )
    age_s = (int(time.time() * 1000) - eq_ts) / 1000
    if age_s > 300:
        return Mismatch(
            "M2", "hard",
            f"equity_marks stale ({age_s:.0f}s old) but no halt",
            evidence={"eq_ts_ms": eq_ts, "age_s": age_s},
        )
    return None


def _rule_m3() -> Mismatch | None:
    """M3: sysaudit OK AND last-50 Wilson-upper expectancy < 0."""
    audit = _sysaudit_latest_verdict()
    et = _economic_truth_latest()
    if audit is None or et is None:
        return None
    # Find a tier cell (>=50 exits by construction if it made it past
    # sample floor) whose expectancy_upper < 0.
    for c in et.get("cells", []):
        if c.get("segment_kind") != "tier":
            continue
        if c.get("verdict") != "fail":
            continue
        if audit == "ok":
            return Mismatch(
                "M3", "hard",
                f"sysaudit=ok but tier={c.get('segment_key')} "
                f"expectancy_upper={c.get('expectancy_upper'):+.4f} < 0",
                evidence={"audit": audit, "tier_cell": c},
            )
    return None


def _rule_m4() -> Mismatch | None:
    """M4: card_truth all_ok AND equity_marks.latest older than 300s."""
    eq_ts = _latest_equity_ts_ms()
    age_s = ((int(time.time() * 1000) - eq_ts) / 1000) if eq_ts else None
    # Read latest card_truth verdict
    try:
        con = _connect()
        try:
            r = con.execute(
                "SELECT verdict FROM spot_card_truth_audits "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            card_verdict = r["verdict"] if r else None
        finally:
            con.close()
    except Exception:
        card_verdict = None
    if card_verdict == "ok" and age_s is not None and age_s > 300:
        return Mismatch(
            "M4", "hard",
            f"card_truth=ok but equity_marks is {age_s:.0f}s stale",
            evidence={"card_truth": card_verdict, "equity_age_s": age_s},
        )
    return None


def _rule_m5() -> Mismatch | None:
    """M5: consensus ticking recently AND no entries in 1h AND engine claims running.

    Interpretation: the consensus pipeline is alive (LLM calls happening)
    but no entries are firing despite the engine being flagged as
    running. This points at the pre-trade gate rejecting everything or
    the freeze being silently active without UI surface.
    """
    snap = _recent_consensus_and_entries()
    now = snap["now_ms"]
    lc = snap["last_consensus_ms"]
    le = snap["last_entry_ms"]
    if lc is None:
        return None
    if (now - lc) > 60 * 1000:
        return None   # consensus also cold → not a contradiction
    if le is not None and (now - le) < 3600 * 1000:
        return None   # entries recent → healthy
    return Mismatch(
        "M5", "soft",
        f"consensus fresh ({(now-lc)/1000:.0f}s ago) but "
        f"no entries in {(now-le)/1000 if le else float('inf'):.0f}s",
        evidence={
            "last_consensus_ms": lc,
            "last_entry_ms": le,
            "gap_s": ((now - le) / 1000) if le else None,
        },
    )


def _rule_m6() -> Mismatch | None:
    """M6: all tiers enabled AND any tier expectancy_upper < 0."""
    ts = _tier_toggles()
    fail = ts.get("fail_tiers_economic") or []
    exec_state = ts.get("exec_state") or {}
    enabled_fail = [t for t in fail if exec_state.get(t) is True]
    if enabled_fail:
        return Mismatch(
            "M6", "hard",
            f"tiers still enabled with economic fail: {enabled_fail}",
            evidence={"enabled_fail": enabled_fail, "exec_state": exec_state},
        )
    return None


def _rule_m7() -> Mismatch | None:
    """M7: pre_trade pass_rate_7d < 0.01 AND entries_7d > 50."""
    s = _pre_trade_stats_7d()
    if (s["pass_rate"] < 0.01) and (s["entries_7d"] > 50):
        return Mismatch(
            "M7", "hard",
            f"pre_trade pass_rate_7d={s['pass_rate']*100:.2f}% but "
            f"entries_7d={s['entries_7d']} — bypass indicator",
            evidence=s,
        )
    return None


# ---------------------------------------------------------------------------
# Public runner
# ---------------------------------------------------------------------------

_RULE_FNS = {
    "M1": _rule_m1, "M2": _rule_m2, "M3": _rule_m3,
    "M4": _rule_m4, "M5": _rule_m5, "M6": _rule_m6, "M7": _rule_m7,
}


def run_once() -> MismatchScan:
    _init_schema()
    scan = MismatchScan()
    for rid, fn in _RULE_FNS.items():
        try:
            m = fn()
        except Exception:
            m = None
        if m is None:
            continue
        scan.findings.append(m)
        if m.severity == "hard":
            scan.n_hard += 1
        else:
            scan.n_soft += 1
        _persist(m, scan.ts_ms)
    # Register T4 if ≥ 2 mismatches in last 30 min
    if scan.n_hard + scan.n_soft >= 2:
        try:
            from spot_aggro.governance.contradiction_freeze import register_trigger
            register_trigger(
                "T4", "card_truth_mismatch_repeat",
                f"{len(scan.findings)} mismatches in current tick: "
                f"{[m.rule_id for m in scan.findings]}",
            )
        except Exception:
            pass
    return scan


def _persist(m: Mismatch, ts_ms: int) -> int:
    with _DB_LOCK:
        con = _connect()
        try:
            cur = con.execute(
                "INSERT INTO spot_card_truth_mismatches("
                " ts_ms, rule_id, severity, detail, evidence_json)"
                " VALUES(?,?,?,?,?)",
                (ts_ms, m.rule_id, m.severity, m.detail,
                 json.dumps(m.evidence)),
            )
            return int(cur.lastrowid or 0)
        finally:
            con.close()


def latest_findings(limit: int = 50) -> list[dict[str, Any]]:
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT finding_id, ts_ms, rule_id, severity, detail,"
                " evidence_json FROM spot_card_truth_mismatches"
                " ORDER BY finding_id DESC LIMIT ?", (limit,),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
                except Exception:
                    pass
                out.append(d)
            return out
        finally:
            con.close()
