"""Phase 11b final — tier persistence end-to-end regression tests.

Root cause fix validation:
  1. trade_log now has a top-level `tier` column.
  2. log_trade() accepts `tier=` explicitly and also derives from payload / module.
  3. Schema migration backfills historical rows in-place (idempotent).
  4. /spot_aggro/ops/trades route returns `tier` as a top-level response field.
  5. Reconciled positions land on tier="?" and are routed to Lane 3 in the
     heatmap — never mixed into A+/A/B/C.
  6. The new heatmap HTML renders three explicit lanes.

These assertions are the contract that prevents "tier silently goes missing"
regressions. If any of them fails, the heatmap can no longer be both truthful
AND operationally useful — which is the success condition the operator set.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
INDEX = REPO / "web" / "ops" / "index.html"


# ---------------------------------------------------------------------------
# Persistence layer — schema + migration + log_trade()
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Fresh, isolated trades DB for each test. Resets persist._initialized
    so init_schema() actually runs the migration path against the tmp DB."""
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    # Reset the module-level init guard so init_schema re-runs.
    from openclaw_v1.shared.persistence import state as persist
    persist._initialized = False
    return db


def test_schema_has_tier_column(fresh_db):
    from openclaw_v1.shared.persistence import state as persist
    persist.init_schema()
    con = sqlite3.connect(fresh_db)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(trade_log)").fetchall()]
    finally:
        con.close()
    assert "tier" in cols, "trade_log must expose tier as a first-class column"


def test_log_trade_explicit_tier_persists(fresh_db):
    from openclaw_v1.shared.persistence import state as persist
    persist.log_trade(
        symbol="TEST-USDT", module="M1_flow_B", action="enter",
        tier="B", payload={"composite": 0.42},
    )
    con = sqlite3.connect(fresh_db)
    try:
        row = con.execute("SELECT tier, module, action FROM trade_log").fetchone()
    finally:
        con.close()
    assert row == ("B", "M1_flow_B", "enter")


def test_log_trade_derives_tier_from_payload(fresh_db):
    from openclaw_v1.shared.persistence import state as persist
    # Caller forgets to pass tier= but did put it in payload (legacy pattern).
    persist.log_trade(
        symbol="TEST-USDT", module="M1_flow_B", action="reject",
        payload={"error": "nope", "tier": "B"},
    )
    con = sqlite3.connect(fresh_db)
    try:
        row = con.execute("SELECT tier FROM trade_log").fetchone()
    finally:
        con.close()
    assert row[0] == "B"


def test_log_trade_derives_tier_from_module_pattern(fresh_db):
    from openclaw_v1.shared.persistence import state as persist
    # No tier=, no payload["tier"] — must fall back to module prefix.
    cases = [
        ("M1_squeeze_A",    "A"),
        ("M1_flow_B",       "B"),
        ("M1_scalp_C",      "C"),
        ("M3_blitz",        "A+"),
        ("M_reconciled",    "?"),
        ("M_reconciled_lowconf", "?"),
    ]
    for module, expected_tier in cases:
        persist.log_trade(
            symbol=f"SYM-{module}", module=module, action="reject",
            payload={"error": "no-tier-provided"},
        )
    con = sqlite3.connect(fresh_db)
    try:
        rows = con.execute("SELECT module, tier FROM trade_log ORDER BY id").fetchall()
    finally:
        con.close()
    assert dict(rows) == dict(cases), f"module-pattern fallback mismatch: {rows}"


def test_log_trade_reject_path_now_persists_tier(fresh_db):
    """Root-cause regression: the pre-fix reject call site wrote
    `payload={"error": ...}` with no tier, causing 469 live rows to have
    tier="?" in payload_json. After the fix, reject rows must carry tier
    either via explicit kwarg or via the module-pattern fallback."""
    from openclaw_v1.shared.persistence import state as persist
    # Simulate the exact engine call site after the fix (engine.py:~646).
    persist.log_trade(
        symbol="ALGO-USDT", module="M1_flow_B", action="reject",
        tier="B",  # ← the fix
        payload={"error": "insufficient balance", "tier": "B", "composite": 0.55},
    )
    con = sqlite3.connect(fresh_db)
    try:
        row = con.execute(
            "SELECT tier, action, module FROM trade_log "
            "WHERE action='reject'"
        ).fetchone()
    finally:
        con.close()
    assert row == ("B", "reject", "M1_flow_B")


# ---------------------------------------------------------------------------
# Backfill migration — existing DBs must pick up tier values from payload/module
# ---------------------------------------------------------------------------

def test_migration_backfills_tier_from_payload(tmp_path, monkeypatch):
    """A DB that was created before the tier column existed must get the
    column added AND existing rows populated from payload_json.tier."""
    db = tmp_path / "legacy_trades.db"
    # Simulate a pre-migration DB: create the old-shape table by hand.
    con = sqlite3.connect(db)
    con.execute("""
        CREATE TABLE trade_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            module TEXT NOT NULL,
            action TEXT NOT NULL,
            side TEXT,
            notional_usd REAL,
            avg_px REAL,
            fee_usd REAL,
            pnl_usd REAL,
            correlation_id TEXT,
            payload_json TEXT
        )
    """)
    con.execute(
        "INSERT INTO trade_log (ts_ms, symbol, module, action, payload_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (1, "BTC-USDT", "M1_flow_B", "reject", json.dumps({"tier": "B", "error": "nope"})),
    )
    con.execute(
        "INSERT INTO trade_log (ts_ms, symbol, module, action, payload_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (2, "ETH-USDT", "M1_scalp_C", "skip", json.dumps({"tier": "C"})),
    )
    # Row that doesn't even have payload.tier — must be recovered via module pattern.
    con.execute(
        "INSERT INTO trade_log (ts_ms, symbol, module, action, payload_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (3, "SOL-USDT", "M_reconciled", "exit", "{}"),
    )
    con.commit()
    con.close()

    # Point the persist module at this legacy DB and run init_schema.
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    from openclaw_v1.shared.persistence import state as persist
    persist._initialized = False
    persist.init_schema()

    con = sqlite3.connect(db)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(trade_log)").fetchall()]
        rows = con.execute(
            "SELECT symbol, module, tier FROM trade_log ORDER BY id"
        ).fetchall()
    finally:
        con.close()
    assert "tier" in cols
    assert rows == [
        ("BTC-USDT", "M1_flow_B",    "B"),   # from payload.tier
        ("ETH-USDT", "M1_scalp_C",   "C"),   # from payload.tier
        ("SOL-USDT", "M_reconciled", "?"),   # from module pattern
    ]


def test_migration_is_idempotent(fresh_db):
    """Running init_schema twice must not error and must not double-apply."""
    from openclaw_v1.shared.persistence import state as persist
    persist.init_schema()
    persist._initialized = False
    persist.init_schema()  # second call — must not raise
    con = sqlite3.connect(fresh_db)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(trade_log)").fetchall()]
    finally:
        con.close()
    # tier appears exactly once.
    assert cols.count("tier") == 1


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------

def test_api_trades_route_selects_tier_column():
    """/spot_aggro/ops/trades must include `tier` in its SELECT so the dashboard can
    read it as a top-level field without regex-extracting payload_json."""
    src = (REPO / "openclaw_v1" / "spot_aggro" / "ops" / "routes_ops.py").read_text(
        encoding="utf-8"
    )
    # The SELECT statement in ops_trades() must include `tier`.
    assert "payload_json, tier" in src, (
        "/spot_aggro/ops/trades SELECT must include the tier column (Phase 11b)"
    )


# ---------------------------------------------------------------------------
# Heatmap HTML — 3-lane design assertions
# ---------------------------------------------------------------------------

def _index() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_heatmap_card_declares_three_lanes():
    s = _index()
    assert "Lane 1 · Canonical performance" in s
    assert "Lane 2 · Canonical activity" in s
    assert "Lane 3 · Reconciliation activity" in s


def test_heatmap_has_three_tbodies():
    s = _index()
    assert 'id="tb-heatmap"' in s
    assert 'id="tb-heatmap-activity"' in s
    assert 'id="tb-heatmap-reconciled"' in s


def test_heatmap_has_unknown_provenance_lane():
    s = _index()
    assert 'id="tb-heatmap-unknown"' in s
    assert "Unknown provenance" in s


def test_heatmap_binds_to_top_level_tier_field():
    """The aggregation loop must read `t.tier` (the new column) as its
    primary source, with payload_json regex only as a pre-migration
    fallback."""
    s = _index()
    # The new code path.
    assert "let tier = t.tier;" in s
    # And the reconciled routing is by module, not tier.
    assert 'module.startsWith("M_reconciled")' in s


def test_heatmap_no_longer_fails_open_with_off_enum_row():
    """The pre-Phase-11b "OFF-ENUM / NON-TIER" inline row is replaced by a
    dedicated Lane 3 section. That inline row must be gone so reconciled
    exits no longer appear inside the canonical table."""
    s = _index()
    # The old inline row header is removed.
    assert "Reconciliation-generated (NOT canonical tier activity" not in s


def test_heatmap_canonical_tiers_always_visible():
    """Operator lock: A+/A/B/C must always render, even with zero data."""
    s = _index()
    assert 'CANONICAL_TIERS = ["A+", "A", "B", "C"]' in s
    # Pre-seeding loop still present.
    assert "for (const t of CANONICAL_TIERS)" in s


def test_heatmap_activity_lane_counts_rejects_and_skips():
    """Lane 2 must tally rejects AND skips per tier — that's what makes the
    card operationally useful when no closed exits exist yet."""
    s = _index()
    assert 'action === "reject"' in s and 's.rejects++' in s
    assert 'action === "skip"' in s and 's.skips++' in s


def test_heatmap_summary_narrates_all_three_lanes():
    """updateHeatmapReport must emit Lane 1 + Lane 2 + Lane 3 text so the
    summary is never empty when any lane has data."""
    s = _index()
    assert "Lane 1 · Closed performance" in s
    assert "Lane 2 · Pre-exit flow" in s
    assert "Lane 3 · Reconciled activity" in s


def test_heatmap_summary_reads_from_three_tbodies():
    s = _index()
    assert 'document.querySelectorAll("#tb-heatmap tr")' in s
    assert 'document.querySelectorAll("#tb-heatmap-activity tr")' in s
    assert 'document.querySelectorAll("#tb-heatmap-reconciled tr")' in s


def test_heatmap_filters_to_spot_modules_only():
    """Phase 11b — /spot_aggro/ops/trades returns rows from BOTH engines (spot and
    the legacy apex_omega perp modules). The Tier Accuracy Heatmap is a
    SPOT AGGRO card per operator lock; perp modules (M1_funding,
    M2_statarb, M3_triangular) must be filtered out so they never pollute
    canonical tier analytics, reject counts, or the Unknown lane."""
    s = _index()
    # Explicit spot-module whitelist in the aggregation.
    assert "SPOT_MODULE_PREFIXES" in s
    # The four spot prefixes must all be listed.
    for prefix in ('"M1_squeeze"', '"M1_flow"', '"M1_scalp"', '"M3_blitz"',
                   '"M_reconciled"'):
        assert prefix in s, f"spot module prefix missing from heatmap whitelist: {prefix}"
    # Filter applied to rows before aggregation.
    assert ".filter(r => isSpotModule(r.module))" in s


def test_tier_column_backfill_handles_perp_modules_as_null():
    """The shared trade_log carries rows from BOTH engines.
    Perp-only modules (M1_funding, M2_statarb, M3_triangular) do not
    match any spot-tier pattern, so the backfill must leave their `tier`
    as NULL — NOT falsely inject a canonical tier label, and NOT crash.
    The heatmap filters them out at the client; the Unknown lane stays
    empty because the filter removes them before categorization."""
    import sqlite3
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "legacy.db")
        con = sqlite3.connect(db)
        con.execute("""
            CREATE TABLE trade_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_ms INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                module TEXT NOT NULL,
                action TEXT NOT NULL,
                side TEXT, notional_usd REAL, avg_px REAL, fee_usd REAL,
                pnl_usd REAL, correlation_id TEXT, payload_json TEXT
            )
        """)
        for i, module in enumerate(["M1_funding", "M2_statarb", "M3_triangular"]):
            con.execute(
                "INSERT INTO trade_log (ts_ms, symbol, module, action, payload_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (i, "BTC-USDT", module, "reject", "{}"),
            )
        con.commit()
        con.close()

        os.environ["TRADE_DB_PATH"] = db
        from openclaw_v1.shared.persistence import state as persist
        persist._initialized = False
        persist.init_schema()

        con = sqlite3.connect(db)
        rows = con.execute(
            "SELECT module, tier FROM trade_log ORDER BY id"
        ).fetchall()
        con.close()
        # All perp modules land with tier=NULL — no false canonical assignment.
        assert all(r[1] is None for r in rows), (
            f"perp modules must NOT be assigned canonical tiers: {rows}"
        )
