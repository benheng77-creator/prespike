"""Opportunity Fabric — Sprint 1 tests: Exploration Wallet."""
from __future__ import annotations

import importlib
import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture
def _iso_wallet(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    monkeypatch.setenv("SPOT_EXPLORATION_WALLET_USD", "30")
    monkeypatch.setenv("SPOT_EXPLORATION_DD_KILL_USD", "5")
    monkeypatch.setenv("SPOT_EXPLORATION_VARIANTS", "contrarian,deep_value")
    import spot_aggro.governance.exploration_wallet as ew
    importlib.reload(ew)
    ew._init_schema()
    # Bootstrap companion table the module reads.
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE IF NOT EXISTS spot_live_variant_entries("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " variant TEXT, status TEXT,"
        " notional_usd REAL, realized_pnl_usd REAL,"
        " opened_ts_ms INTEGER, closed_ts_ms INTEGER)"
    )
    con.commit()
    con.close()
    yield db, ew


def _seed_exit(db: Path, variant: str, pnl: float,
               closed_ts_ms: int | None = None) -> None:
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO spot_live_variant_entries("
        " variant, status, notional_usd, realized_pnl_usd, closed_ts_ms)"
        " VALUES(?,?,?,?,?)",
        (variant, "closed", 25.0, pnl,
         closed_ts_ms if closed_ts_ms is not None else int(time.time() * 1000)),
    )
    con.commit()
    con.close()


def test_default_off_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("SPOT_EXPLORATION_WALLET_USD", raising=False)
    monkeypatch.delenv("SPOT_EXPLORATION_VARIANTS", raising=False)
    import spot_aggro.governance.exploration_wallet as ew
    importlib.reload(ew)
    assert ew.is_enabled() is False
    # filter_variants must be a no-op when disabled.
    assert ew.filter_variants(("contrarian", "momentum")) == ("contrarian", "momentum")


def test_enabled_when_env_set(_iso_wallet):
    _, ew = _iso_wallet
    assert ew.is_enabled() is True
    st = ew.state()
    assert st.allocation_usd == 30.0
    assert st.dd_kill_threshold_usd == 5.0
    assert st.funded_variants == ["contrarian", "deep_value"]
    assert st.disabled is False


def test_pnl_24h_computed_from_recent_exits(_iso_wallet):
    db, ew = _iso_wallet
    now_ms = int(time.time() * 1000)
    # Inside 24h window.
    _seed_exit(db, "contrarian", pnl=-2.0, closed_ts_ms=now_ms - 3_600_000)
    _seed_exit(db, "deep_value", pnl=+0.5, closed_ts_ms=now_ms - 7_200_000)
    # Outside 24h window (older).
    _seed_exit(db, "contrarian", pnl=+10.0, closed_ts_ms=now_ms - 48 * 3_600_000)
    st = ew.state()
    assert abs(st.pnl_24h_usd - (-1.5)) < 0.01
    assert abs(st.realized_pnl_usd - 8.5) < 0.01


def test_dd_kill_fires_when_24h_loss_breaches_threshold(_iso_wallet):
    db, ew = _iso_wallet
    now_ms = int(time.time() * 1000)
    _seed_exit(db, "contrarian", pnl=-3.0, closed_ts_ms=now_ms - 3_600_000)
    _seed_exit(db, "deep_value", pnl=-3.0, closed_ts_ms=now_ms - 2 * 3_600_000)
    rpt = ew.evaluate()
    assert rpt["ok"] is True
    assert rpt["action"] == "killed"
    st = ew.state()
    assert st.disabled is True
    reason = st.last_kill_reason or ""
    assert "$-6.00" in reason and "disabling" in reason


def test_dd_kill_does_not_fire_when_pnl_above_threshold(_iso_wallet):
    db, ew = _iso_wallet
    now_ms = int(time.time() * 1000)
    _seed_exit(db, "contrarian", pnl=-1.5, closed_ts_ms=now_ms - 3_600_000)
    rpt = ew.evaluate()
    assert rpt["action"] == "healthy"
    assert ew.state().disabled is False


def test_filter_variants_strips_funded_when_disabled(_iso_wallet):
    db, ew = _iso_wallet
    now_ms = int(time.time() * 1000)
    _seed_exit(db, "contrarian", pnl=-6.0, closed_ts_ms=now_ms - 3_600_000)
    ew.evaluate()          # trip the kill
    filtered = ew.filter_variants(("contrarian", "deep_value", "momentum"))
    assert "contrarian" not in filtered
    assert "deep_value" not in filtered
    assert "momentum" in filtered           # conservative variant preserved


def test_operator_reset_re_enables_wallet(_iso_wallet):
    db, ew = _iso_wallet
    now_ms = int(time.time() * 1000)
    _seed_exit(db, "contrarian", pnl=-6.0, closed_ts_ms=now_ms - 3_600_000)
    ew.evaluate()
    assert ew.state().disabled is True
    r = ew.reset(operator="Ben")
    assert r["ok"] is True
    assert ew.state().disabled is False


def test_no_auto_reset_when_24h_pnl_recovers(_iso_wallet):
    """Once killed, the wallet stays disabled until operator action,
    even if 24h PnL window rolls forward and new exits push the recent
    PnL above the threshold."""
    db, ew = _iso_wallet
    now_ms = int(time.time() * 1000)
    # Old kill event 30h ago (so no longer in the rolling 24h window).
    _seed_exit(db, "contrarian", pnl=-10.0, closed_ts_ms=now_ms - 30 * 3_600_000)
    # Recent small loss.
    _seed_exit(db, "contrarian", pnl=-0.2, closed_ts_ms=now_ms - 3_600_000)
    # Manually mark wallet disabled (simulates previous kill).
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT OR REPLACE INTO spot_exploration_wallet_state("
        " id, allocation_usd, realized_pnl_usd,"
        " disabled_until_ts_ms, last_updated_ts_ms,"
        " last_kill_ts_ms, last_kill_reason)"
        " VALUES(1, ?, ?, 0, ?, ?, ?)",
        (30.0, -10.2, now_ms - 3600_000, now_ms - 3600_000, "prior kill"),
    )
    con.commit()
    con.close()
    rpt = ew.evaluate()
    assert rpt["action"] == "disabled_awaiting_operator"
    assert ew.state().disabled is True


def test_events_table_records_kill_and_reset(_iso_wallet):
    db, ew = _iso_wallet
    now_ms = int(time.time() * 1000)
    _seed_exit(db, "contrarian", pnl=-6.0, closed_ts_ms=now_ms - 3_600_000)
    ew.evaluate()
    ew.reset(operator="test")
    events = ew.recent_events(limit=20)
    kinds = [e["kind"] for e in events]
    assert "kill" in kinds
    assert "reset" in kinds
