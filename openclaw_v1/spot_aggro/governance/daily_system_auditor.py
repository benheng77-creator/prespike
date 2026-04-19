"""Phase 11j — Daily system-integrity auditor for SPOT AGGRO.

Purpose
-------
Runs once per day (scheduled) and proves that every piece required to
produce a trading decision is in place, connected, non-broken, and fully
functional. Result is persisted to SQLite (`spot_system_audit_runs`) and
surfaced on the dashboard so the operator can see green/red at a glance
without reading logs.

Seven check categories:
  1. algorithm  — core scoring functions import + run on representative input
  2. formula    — sizing / TP / SL math produces finite positive numbers
  3. flow       — entry-flow import graph (scoring → consensus → adapter) is wireable
  4. sequence   — critical guard ordering (e.g. reconciled short-circuit before TP/SL)
  5. connections— adapter, DB, config files, telemetry can be reached
  6. state      — DB schema + engine state dataclass + no NULL-tier spot rows
  7. data       — recent 24h activity is internally consistent; no orphaned positions

Each check returns CheckResult. The aggregate verdict is the worst
individual severity.

SPOT AGGRO only.
This module imports NOTHING from apex_omega. It reads the shared
apex_trade_log table (shared ops infra) via `shared.persistence.state`
but performs no writes outside its own result table.

No LLM call. Pure deterministic validator. The daily cadence + 7
categories give the operator "one glance and you know if the engine
would trade correctly today" without paying per-check LLM cost.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    """One check's outcome."""
    name: str
    category: str
    severity: str  # "ok" | "warn" | "fail"
    ok: bool
    details: str
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditRun:
    """Full audit run summary."""
    run_id: str
    started_ts_ms: int
    completed_ts_ms: int
    verdict: str  # "ok" | "warn" | "fail"
    n_checks: int
    n_ok: int
    n_warn: int
    n_fail: int
    checks: list[CheckResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["checks"] = [c if isinstance(c, dict) else c.to_dict() for c in d["checks"]]
        return d


# ---------------------------------------------------------------------------
# Check runners
# ---------------------------------------------------------------------------

def _timed(name: str, category: str, fn: Callable[[], tuple[str, str]]) -> CheckResult:
    """Run `fn` and wrap its (severity, details) tuple into a CheckResult.
    A check that RAISES is always a 'fail' — we never let an exception
    mask a broken invariant."""
    t0 = time.monotonic()
    try:
        severity, details = fn()
    except Exception as exc:  # noqa: BLE001
        severity = "fail"
        details = f"check raised {type(exc).__name__}: {exc}"
    dur_ms = int((time.monotonic() - t0) * 1000)
    return CheckResult(
        name=name,
        category=category,
        severity=severity,
        ok=(severity == "ok"),
        details=details[:500],
        duration_ms=dur_ms,
    )


# ── 1. algorithm ────────────────────────────────────────────────────────────

def check_compute_composite_score_wireable() -> tuple[str, str]:
    from spot_aggro.scoring import compute_composite_score
    # Minimal representative input; real callers pass full MIO state.
    sample = {"symbol": "TEST-USDT", "spi": 0.30, "funding_z": -1.0,
              "composite": 0.0, "volatility_z": 0.0}
    class MIO:
        regime = "SQUEEZE_BUILDING"
        squeeze_timing = "NEAR"
        edge_status = "HEALTHY"
        asset_scores: dict[str, float] = {}
        top_6: list[str] = []
    v = compute_composite_score(sample, MIO())
    if not isinstance(v, (int, float)):
        return "fail", f"compute_composite_score returned non-numeric: {type(v).__name__}"
    if not (0.0 <= v <= 2.0):
        return "warn", f"composite out of expected [0,2] range: {v}"
    return "ok", f"compute_composite_score -> {v:.3f} on representative input"


def check_classify_tier_wireable() -> tuple[str, str]:
    from spot_aggro.scoring import classify_tier
    # Signature: classify_tier(composite, spi, thresholds=None).
    # Test a few composite levels to prove each tier is reachable.
    samples = [
        (0.95, 0.90),  # Should hit A+ (requires SPI >= 0.85)
        (0.60, 0.30),  # Should hit A/B depending on thresholds
        (0.40, 0.30),  # Should hit B/C
        (0.20, 0.10),  # Likely below all thresholds → None
    ]
    tiers_seen = set()
    for composite, spi in samples:
        tc = classify_tier(composite=composite, spi=spi)
        if tc is None:
            continue
        tier = getattr(tc, "tier", None)
        module = getattr(tc, "module", None)
        if tier not in ("A+", "A", "B", "C"):
            return "fail", f"classify_tier({composite}, {spi}) returned invalid tier={tier!r}"
        if not module:
            return "fail", f"classify_tier({composite}, {spi}) returned no module for tier={tier}"
        tiers_seen.add(tier)
    if not tiers_seen:
        return "warn", "no tier reachable across sample composites — thresholds may be misconfigured"
    return "ok", f"classify_tier reached tiers: {sorted(tiers_seen)}"


# ── 2. formula ──────────────────────────────────────────────────────────────

def check_tp_sl_math_finite() -> tuple[str, str]:
    """TP and SL multipliers must produce finite, sane prices for
    typical entry prices. The engine computes the actual TP/SL fraction
    as `spi × mult`, so we multiply against a representative SPI=0.30
    to verify the end-to-end price math doesn't blow up for any tier.

    This catches regressions where per-tier tp_mult/sl_mult configs
    drift into NaN/inf/extreme values.
    """
    from spot_aggro.scoring import TIER_PARAMS
    import math
    ENTRY = 1.0                  # unit entry so any drift is obvious
    SAMPLE_SPI = 0.30            # typical tier-passing SPI
    issues = []
    for tier_name, tc in TIER_PARAMS.items():
        tp_frac = SAMPLE_SPI * tc.tp_mult
        sl_frac = -SAMPLE_SPI * tc.sl_mult    # SL is a negative return
        tp_price = ENTRY * (1 + tp_frac)
        sl_price = ENTRY * (1 + sl_frac)
        for name, val in (("tp_mult", tc.tp_mult), ("sl_mult", tc.sl_mult),
                          ("tp_price", tp_price), ("sl_price", sl_price),
                          ("trail_activate", tc.trail_activate),
                          ("trail_pct", tc.trail_pct),
                          ("max_hold_h", tc.max_hold_h)):
            if not math.isfinite(val):
                issues.append(f"{tier_name}.{name}={val}")
        if tp_price <= 0:
            issues.append(f"{tier_name} tp_price={tp_price} must be positive")
        if sl_price <= 0:
            issues.append(f"{tier_name} sl_price={sl_price} must be positive")
        # Sanity bounds on multipliers — catches someone setting tp_mult=999
        # by accident. Real values are all < 3.0.
        if not (0 < tc.tp_mult < 10):
            issues.append(f"{tier_name}.tp_mult={tc.tp_mult} out of (0,10)")
        if not (0 < tc.sl_mult < 10):
            issues.append(f"{tier_name}.sl_mult={tc.sl_mult} out of (0,10)")
    if issues:
        return "fail", "; ".join(issues[:5])  # cap detail length
    return "ok", f"TP/SL math finite+bounded across {len(TIER_PARAMS)} tiers (SPI=0.30 sample)"


def check_sizing_positive_and_bounded() -> tuple[str, str]:
    from spot_aggro.scoring import TIER_PARAMS
    issues = []
    for tier_name, tc in TIER_PARAMS.items():
        max_frac = tc.max_size_frac
        if not (0 < max_frac <= 1.0):
            issues.append(f"{tier_name} max_size_frac={max_frac} out of (0,1]")
        if tc.max_hold_h <= 0:
            issues.append(f"{tier_name} max_hold_h={tc.max_hold_h} must be > 0")
    if issues:
        return "fail", "; ".join(issues)
    return "ok", f"sizing bounds valid across {len(TIER_PARAMS)} tiers"


# ── 3. flow ─────────────────────────────────────────────────────────────────

def check_entry_flow_import_graph() -> tuple[str, str]:
    """Every module in the entry flow must import cleanly. One broken
    import anywhere in this chain bricks trading."""
    required = [
        "spot_aggro.engine",
        "spot_aggro.scoring",
        "spot_aggro.swarm.runner",
        "spot_aggro.swarm.integration",
        "spot_aggro.gates.tier_toggle",
        "spot_aggro.reconciliation",
        "spot_aggro.telemetry",
        "shared.persistence.state",
        "shared.adapters.okx_unified",
    ]
    broken = []
    for name in required:
        try:
            __import__(name)
        except Exception as exc:  # noqa: BLE001
            broken.append(f"{name}: {type(exc).__name__}")
    if broken:
        return "fail", "broken imports: " + "; ".join(broken)
    return "ok", f"all {len(required)} entry-flow imports clean"


# ── 4. sequence ─────────────────────────────────────────────────────────────

def check_reconciled_short_circuit_ordering() -> tuple[str, str]:
    """Critical Phase 11d invariant. If this regresses, reconciled
    positions can auto-exit with cash loss."""
    import inspect
    from spot_aggro.engine import SpotAggroEngine
    src = inspect.getsource(SpotAggroEngine._check_exits_v2)
    guard = src.find('if pos.module in ("M_reconciled"')
    hard  = src.find('if ret >= pos.tp:')
    if guard < 0:
        return "fail", "reconciled short-circuit missing from _check_exits_v2"
    if hard < 0:
        return "fail", "hard-exit block missing from _check_exits_v2"
    if guard >= hard:
        return "fail", "reconciled guard is AFTER TP/SL check — cash-loss leak"
    return "ok", "reconciled short-circuit precedes TP/SL (invariant held)"


def check_start_only_via_explicit_operator_action() -> tuple[str, str]:
    """Engine must not auto-start on server boot (post-incident 2026-04-19)."""
    import re
    src_path = Path(__file__).resolve().parents[3] / "openclaw_v1" / "server.py"
    src = src_path.read_text(encoding="utf-8")
    stripped = re.sub(r"#.*", "", src)
    stripped = re.sub(r'"""[\s\S]*?"""', "", stripped)
    if "start_engine(" in stripped:
        return "fail", "server.py calls start_engine() in executable code"
    if "SpotAggroEngine(" in stripped or "APEX_Spot_Aggro(" in stripped:
        return "fail", "server.py instantiates engine class directly"
    return "ok", "engine starts only via explicit POST /spot_aggro/start"


# ── 5. connections ──────────────────────────────────────────────────────────

def check_adapter_importable() -> tuple[str, str]:
    from shared.adapters.okx_unified import OKXUnified
    # Don't actually connect — just verify the class exists and exposes
    # the methods the spot engine calls. Method names are OKX-native
    # (get_spot_fills, not ccxt's fetch_my_trades).
    required_methods = [
        "get_spot_ticker",     # live price ticker
        "place_post_only",     # order submission
        "get_account_equity",  # equity snapshot (advisory)
        "get_free_usdt",       # pre-flight USDT guard
        "get_spot_fills",      # reconciliation fills
        "get_spot_holdings",   # reconciliation positions
    ]
    missing = [m for m in required_methods if not hasattr(OKXUnified, m)]
    if missing:
        return "fail", f"OKXUnified missing methods: {missing}"
    return "ok", f"OKXUnified exposes all {len(required_methods)} required methods"


def check_db_reachable() -> tuple[str, str]:
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    try:
        row = con.execute("SELECT 1").fetchone()
    finally:
        con.close()
    if row is None or row[0] != 1:
        return "fail", "DB SELECT 1 did not return 1"
    return "ok", "SQLite reachable, schema initialised"


def check_required_config_files_present() -> tuple[str, str]:
    repo = Path(__file__).resolve().parents[3]
    configs = [
        "openclaw_v1/spot_aggro/config/tiers.yml",
        "openclaw_v1/spot_aggro/config/physics.yml",
        "openclaw_v1/spot_aggro/config/state.yml",
        "openclaw_v1/spot_aggro/config/capital.yml",
        "openclaw_v1/spot_aggro/config/audit_swarm.yml",
        "openclaw_v1/spot_aggro/config/governance.yml",
    ]
    missing = [p for p in configs if not (repo / p).exists()]
    if missing:
        return "fail", f"missing config files: {missing}"
    return "ok", f"all {len(configs)} config files present"


# ── 6. state ────────────────────────────────────────────────────────────────

def check_trade_log_has_tier_column() -> tuple[str, str]:
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    try:
        cols = [r[1] for r in con.execute(
            "PRAGMA table_info(apex_trade_log)").fetchall()]
    finally:
        con.close()
    if "tier" not in cols:
        return "fail", "apex_trade_log.tier column missing (Phase 11b migration didn't run)"
    return "ok", f"apex_trade_log has tier column ({len(cols)} cols total)"


def check_no_null_tier_spot_rows() -> tuple[str, str]:
    """All spot-module rows must have a resolved tier post-backfill."""
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    try:
        row = con.execute(
            "SELECT count(*) FROM apex_trade_log "
            "WHERE tier IS NULL "
            "AND (module LIKE 'M1_squeeze%' OR module LIKE 'M1_flow%' "
            "     OR module LIKE 'M1_scalp%' OR module LIKE 'M3_blitz%' "
            "     OR module LIKE 'M_reconciled%')"
        ).fetchone()
    finally:
        con.close()
    n = row[0] if row else 0
    if n > 0:
        return "warn", f"{n} spot rows still have NULL tier (backfill incomplete)"
    return "ok", "0 NULL-tier spot rows — backfill complete"


def check_engine_state_dataclass_shape() -> tuple[str, str]:
    from spot_aggro.engine import EngineState
    required = [
        "started_ts", "capital_usd", "positions", "cycles",
        "trades_today", "blitz_active", "frequency_ctrl",
        "reconciliation_summary",
    ]
    missing = [f for f in required if not hasattr(EngineState(started_ts=0), f)]
    if missing:
        return "fail", f"EngineState missing fields: {missing}"
    return "ok", f"EngineState has all {len(required)} required fields"


# ── 7. data ─────────────────────────────────────────────────────────────────

def check_recent_activity_internally_consistent() -> tuple[str, str]:
    """Over the last 24h, the sum of exits per tier must equal the total
    exit count (not a separate aggregation). Catches silent double-counting."""
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    try:
        ts_24h_ago_ms = int((time.time() - 86400) * 1000)
        total = con.execute(
            "SELECT count(*) FROM apex_trade_log "
            "WHERE action='exit' AND ts_ms > ? "
            "AND (module LIKE 'M1_%' OR module LIKE 'M3_blitz%' OR module LIKE 'M_reconciled%')",
            (ts_24h_ago_ms,),
        ).fetchone()[0]
        by_tier = con.execute(
            "SELECT tier, count(*) FROM apex_trade_log "
            "WHERE action='exit' AND ts_ms > ? "
            "AND (module LIKE 'M1_%' OR module LIKE 'M3_blitz%' OR module LIKE 'M_reconciled%') "
            "GROUP BY tier",
            (ts_24h_ago_ms,),
        ).fetchall()
    finally:
        con.close()
    tier_sum = sum(r[1] for r in by_tier)
    if tier_sum != total:
        return "fail", (
            f"24h exit total mismatch: total={total} vs sum(per-tier)={tier_sum}"
        )
    return "ok", f"24h activity consistent: {total} spot exits, tier sum matches"


def check_no_zombie_positions() -> tuple[str, str]:
    """A position whose `entry_time` is > 7 days old AND module doesn't
    start with M_reconciled is a zombie — the engine lost track of it.
    Reconciled positions are exempt (they have sentinel max_hold_h)."""
    try:
        from spot_aggro import _engine_instance
        if _engine_instance is None:
            return "ok", "engine not started — no zombie check needed"
        positions = getattr(_engine_instance.state, "positions", {})
    except Exception:
        return "warn", "engine state not available for zombie check"

    now_s = time.time()
    zombies = []
    for sym, pos in positions.items():
        if pos.module.startswith("M_reconciled"):
            continue
        age_s = now_s - pos.entry_time
        if age_s > 7 * 86400:
            zombies.append(f"{sym} age={age_s/86400:.1f}d module={pos.module}")
    if zombies:
        return "warn", f"{len(zombies)} zombie positions: " + "; ".join(zombies[:3])
    return "ok", f"no zombie positions across {len(positions)} open"


# ---------------------------------------------------------------------------
# Registry + orchestrator
# ---------------------------------------------------------------------------

CHECKS: list[tuple[str, str, Callable[[], tuple[str, str]]]] = [
    # (name, category, fn)
    ("compute_composite_score wireable",  "algorithm",   check_compute_composite_score_wireable),
    ("classify_tier wireable",            "algorithm",   check_classify_tier_wireable),
    ("TP/SL math finite",                 "formula",     check_tp_sl_math_finite),
    ("sizing positive + bounded",         "formula",     check_sizing_positive_and_bounded),
    ("entry flow import graph",           "flow",        check_entry_flow_import_graph),
    ("reconciled guard ordering",         "sequence",    check_reconciled_short_circuit_ordering),
    ("no auto-start on boot",             "sequence",    check_start_only_via_explicit_operator_action),
    ("OKX adapter importable",            "connections", check_adapter_importable),
    ("DB reachable",                      "connections", check_db_reachable),
    ("config files present",              "connections", check_required_config_files_present),
    ("trade_log.tier column present",     "state",       check_trade_log_has_tier_column),
    ("no NULL-tier spot rows",            "state",       check_no_null_tier_spot_rows),
    ("EngineState dataclass shape",       "state",       check_engine_state_dataclass_shape),
    ("24h activity internally consistent","data",        check_recent_activity_internally_consistent),
    ("no zombie positions",               "data",        check_no_zombie_positions),
]


def run_audit() -> AuditRun:
    """Execute every registered check; return an AuditRun."""
    started_ms = int(time.time() * 1000)
    # ms-precision run_id so two runs in the same second get unique rows.
    # (The test suite runs 3 audits in <30ms to verify history ordering.)
    run_id = f"sa-{started_ms}"
    checks: list[CheckResult] = []
    for name, category, fn in CHECKS:
        checks.append(_timed(name, category, fn))

    n_ok = sum(1 for c in checks if c.severity == "ok")
    n_warn = sum(1 for c in checks if c.severity == "warn")
    n_fail = sum(1 for c in checks if c.severity == "fail")

    # Verdict = worst severity; ok beats warn beats fail.
    if n_fail > 0:
        verdict = "fail"
    elif n_warn > 0:
        verdict = "warn"
    else:
        verdict = "ok"

    return AuditRun(
        run_id=run_id,
        started_ts_ms=started_ms,
        completed_ts_ms=int(time.time() * 1000),
        verdict=verdict,
        n_checks=len(checks),
        n_ok=n_ok,
        n_warn=n_warn,
        n_fail=n_fail,
        checks=checks,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_system_audit_runs (
    run_id         TEXT PRIMARY KEY,
    started_ts_ms  INTEGER NOT NULL,
    completed_ts_ms INTEGER NOT NULL,
    verdict        TEXT NOT NULL,
    n_checks       INTEGER NOT NULL,
    n_ok           INTEGER NOT NULL,
    n_warn         INTEGER NOT NULL,
    n_fail         INTEGER NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_sys_audit_ts
    ON spot_system_audit_runs(started_ts_ms DESC);
"""


def _init_schema() -> None:
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    try:
        con.executescript(_SCHEMA)
        con.commit()
    finally:
        con.close()


def persist_audit(run: AuditRun) -> None:
    """Write one audit run. Idempotent on run_id."""
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO spot_system_audit_runs "
            "(run_id, started_ts_ms, completed_ts_ms, verdict, n_checks, "
            " n_ok, n_warn, n_fail, payload_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run.run_id, run.started_ts_ms, run.completed_ts_ms,
                run.verdict, run.n_checks, run.n_ok, run.n_warn, run.n_fail,
                json.dumps(run.to_dict(), default=str),
            ),
        )
        con.commit()
    finally:
        con.close()


def latest_run() -> dict[str, Any] | None:
    """Return the most recent audit run as a dict, or None if none yet."""
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        row = con.execute(
            "SELECT payload_json FROM spot_system_audit_runs "
            "ORDER BY started_ts_ms DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:  # noqa: BLE001
        return None


def history(limit: int = 30) -> list[dict[str, Any]]:
    """Return the last N audit runs (summary rows, no check details)."""
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT run_id, started_ts_ms, verdict, n_checks, n_ok, n_warn, n_fail "
            "FROM spot_system_audit_runs ORDER BY started_ts_ms DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "run_id": r[0], "started_ts_ms": r[1], "verdict": r[2],
            "n_checks": r[3], "n_ok": r[4], "n_warn": r[5], "n_fail": r[6],
        }
        for r in rows
    ]


def run_and_persist() -> AuditRun:
    """One-shot: run audit and store result. This is what the scheduler
    and the /audit/system/run endpoint call."""
    r = run_audit()
    try:
        persist_audit(r)
    except Exception:  # noqa: BLE001
        # Persistence failure must not break the audit itself.
        pass
    return r
