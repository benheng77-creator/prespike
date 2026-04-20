"""Regression: CDV panel dedup + pipeline funnel correctness.

Before this fix:
  - active_opportunities LEFT-JOINed shadow_variant_exits and fanned
    out a single authz into N rows -> same symbol appeared 3-10x.
  - pipeline counters scanned/shortlisted/approved/rejected all
    derived from the same totals, so they showed identical or doubled
    numbers that didn't reflect real funnel stages.
"""
from __future__ import annotations

import importlib
import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture
def _iso_panel(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    monkeypatch.setenv("FEATURE_CONTRARIAN_DEEPVALUE_PANEL", "1")
    import spot_aggro.api.routes_strategy_cdv as m
    importlib.reload(m)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE IF NOT EXISTS shadow_variant_authorizations("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts_ms INTEGER, variant TEXT, symbol TEXT,"
        " variant_passed INTEGER, variant_score REAL, reason TEXT,"
        " live_authz_id TEXT, side TEXT, tier TEXT)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS shadow_variant_exits("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts_ms INTEGER, variant TEXT, symbol TEXT,"
        " correlation_id TEXT)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS spot_live_variant_entries("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts_ms INTEGER, opened_ts_ms INTEGER, closed_ts_ms INTEGER,"
        " variant TEXT, symbol TEXT, status TEXT,"
        " notional_usd REAL, realized_pnl_usd REAL)"
    )
    con.commit()
    con.close()
    yield db, m


def _seed_authz(db: Path, variant: str, symbol: str, passed: int,
                score: float, age_s: float = 1.0) -> None:
    ts = int(time.time() * 1000) - int(age_s * 1000)
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO shadow_variant_authorizations("
        " ts_ms, variant, symbol, variant_passed, variant_score, reason)"
        " VALUES(?,?,?,?,?,?)",
        (ts, variant, symbol, passed, score,
         f"{variant} {'ADMIT' if passed else 'REJECT'}"),
    )
    con.commit()
    con.close()


def _seed_live_entry(db: Path, variant: str, symbol: str,
                     age_s: float = 60.0) -> None:
    ts = int(time.time() * 1000) - int(age_s * 1000)
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO spot_live_variant_entries("
        " ts_ms, opened_ts_ms, variant, symbol, status, notional_usd)"
        " VALUES(?,?,?,?,?,?)",
        (ts, ts, variant, symbol, "open", 5.0),
    )
    con.commit()
    con.close()


def test_active_opportunities_deduplicates_by_symbol(_iso_panel):
    """Symbol scored 5 times should appear once in active_opportunities."""
    db, m = _iso_panel
    # Score INJ-USDT 5 times for contrarian, all passed.
    for i in range(5):
        _seed_authz(db, "contrarian", "INJ-USDT", passed=1,
                    score=0.75, age_s=i + 1)
    # Score SEI-USDT 3 times.
    for i in range(3):
        _seed_authz(db, "contrarian", "SEI-USDT", passed=1,
                    score=0.80, age_s=i + 1)
    cutoff = int(time.time() * 1000) - 3600 * 1000
    view = m._build_variant_view("contrarian", cutoff)
    syms = [o["symbol"] for o in view["active_opportunities"]]
    # Each symbol appears exactly once.
    assert syms.count("INJ-USDT") == 1
    assert syms.count("SEI-USDT") == 1
    # n_scores reflects the repetition.
    by_sym = {o["symbol"]: o for o in view["active_opportunities"]}
    assert by_sym["INJ-USDT"]["n_scores"] == 5
    assert by_sym["SEI-USDT"]["n_scores"] == 3


def test_variant_view_found_admitted_rejected_consistent(_iso_panel):
    db, m = _iso_panel
    # 10 scoring events, 7 passed 3 rejected.
    for i in range(7):
        _seed_authz(db, "contrarian", f"PASS-{i}", passed=1, score=0.7)
    for i in range(3):
        _seed_authz(db, "contrarian", f"REJ-{i}", passed=0, score=0.3)
    cutoff = int(time.time() * 1000) - 3600 * 1000
    view = m._build_variant_view("contrarian", cutoff)
    assert view["found"] == 10
    assert view["admitted"] == 7
    assert view["rejected"] == 3


def test_pipeline_counters_are_distinct(_iso_panel):
    """scanned != shortlisted != approved when they legitimately differ."""
    db, m = _iso_panel
    # 20 scoring events on 4 unique symbols across 2 variants.
    for sym in ("A", "B", "C", "D"):
        for _ in range(3):           # 3x scored per symbol, all pass
            _seed_authz(db, "contrarian", sym, passed=1, score=0.7)
        for _ in range(2):           # 2x scored per symbol, all reject
            _seed_authz(db, "deep_value", sym, passed=0, score=0.3)
    # Of those 4 admitting symbols, 2 actually reached live entry.
    _seed_live_entry(db, "contrarian", "A")
    _seed_live_entry(db, "contrarian", "B")
    cutoff = int(time.time() * 1000) - 3600 * 1000
    pipeline = m._build_pipeline(cutoff)
    # Scanned = total scoring events = 20.
    assert pipeline["scanned"] == 20
    # Shortlisted = unique (symbol, variant) pairs with passed=1 = 4.
    assert pipeline["shortlisted"] == 4
    # Approved = unique symbols in live_entries = 2.
    assert pipeline["approved"] == 2
    # Rejected = passed=0 scoring events = 4 symbols * 2 scores = 8.
    assert pipeline["rejected"] == 8
    # All four stages should be genuinely distinct (no coincidences).
    assert len({pipeline["scanned"], pipeline["shortlisted"],
                pipeline["approved"], pipeline["rejected"]}) >= 3


def test_pipeline_rejected_counts_only_failures(_iso_panel):
    """Rejected must not count passed=1 rows even when they share a reason."""
    db, m = _iso_panel
    for _ in range(10):
        _seed_authz(db, "contrarian", "X", passed=1, score=0.7)
    cutoff = int(time.time() * 1000) - 3600 * 1000
    pipeline = m._build_pipeline(cutoff)
    assert pipeline["scanned"] == 10
    assert pipeline["rejected"] == 0
