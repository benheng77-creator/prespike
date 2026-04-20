"""Phase 11n-9-mm — 1.5-2% dual-band target tests.

Validates:
  1. strategy_sufficiency exposes TARGET_FLOOR_PCT + TARGET_STRETCH_PCT.
  2. Verdict = 'excellent' when pct_hitting_stretch >= 45% + wilson >= 25%.
  3. Verdict = 'keep' when pct_hitting_floor >= 35% + wilson > 15%.
  4. Verdict = 'replace' when avg_win <= 1.0% (structural ceiling).
  5. Verdict = 'tune' when avg_win > 1.0% but below floor hit rate.
  6. Tier-C TP/SL multipliers updated: tp_mult=1.30, sl_mult=1.40, max_hold_h=4.
  7. Tier-B TP/SL multipliers updated: tp_mult=1.20, sl_mult=1.20, max_hold_h=6.
  8. Build tag + new flags advertised.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import time
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def _iso_db(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    import spot_aggro.governance.strategy_sufficiency as ss
    importlib.reload(ss)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE trade_log("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts_ms INTEGER NOT NULL, symbol TEXT NOT NULL,"
        " module TEXT, action TEXT NOT NULL, tier TEXT,"
        " notional_usd REAL, fee_usd REAL, pnl_usd REAL,"
        " side TEXT, avg_px REAL, correlation_id TEXT,"
        " payload_json TEXT)"
    )
    con.commit()
    con.close()
    yield db


def _seed(db, nets):
    con = sqlite3.connect(str(db))
    now = int(time.time() * 1000)
    for i, net in enumerate(nets):
        ts = now - (len(nets) - i) * 60_000
        sym = f"C{i}-USDT"
        con.execute(
            "INSERT INTO trade_log(ts_ms, symbol, module, action, tier,"
            " notional_usd, fee_usd, pnl_usd, payload_json) VALUES "
            "(?,?, 'M1', 'exit', 'C', 100.0, 0.0, ?, '{}')",
            (ts, sym, net * 100.0),
        )
    con.commit()
    con.close()


def test_band_constants_exposed():
    from spot_aggro.governance import strategy_sufficiency as ss
    assert ss.TARGET_FLOOR_PCT == 0.015
    assert ss.TARGET_STRETCH_PCT == 0.02


def test_excellent_when_stretch_cleared(_iso_db):
    from spot_aggro.governance.strategy_sufficiency import evaluate
    # 30 trades: 15 @ +3%, 10 @ +2.5%, 5 @ -1%
    # pct_hitting_stretch = 25/30 = 83%, pct_hitting_floor = 25/30 = 83%
    nets = [0.03] * 15 + [0.025] * 10 + [-0.01] * 5
    _seed(_iso_db, nets)
    v = evaluate()
    assert v.recommendation == "excellent"
    assert v.sufficient is True


def test_keep_when_floor_cleared_but_not_stretch(_iso_db):
    from spot_aggro.governance.strategy_sufficiency import evaluate
    # 30 trades: 12 @ +1.7% (clears floor not stretch), 18 @ -0.8%
    # pct_hitting_floor = 12/30 = 40%, pct_hitting_stretch = 0
    # wilson_low on 12/30 ≈ 24% which > 15% threshold
    nets = [0.017] * 12 + [-0.008] * 18
    _seed(_iso_db, nets)
    v = evaluate()
    assert v.recommendation == "keep"
    assert v.pct_hitting_stretch == 0.0
    assert v.pct_hitting_target >= 0.35


def test_replace_when_avg_win_below_structural_floor(_iso_db):
    from spot_aggro.governance.strategy_sufficiency import evaluate
    # 25 wins at 0.5% → avg_win = 0.5% < 1.0% structural floor
    nets = [0.005] * 25
    _seed(_iso_db, nets)
    v = evaluate()
    assert v.recommendation == "replace"
    assert "REPLACE" in v.reason.upper() or "STRUCTURAL" in v.reason.upper()


def test_tune_when_avg_above_structural_but_below_floor_hit_rate(_iso_db):
    from spot_aggro.governance.strategy_sufficiency import evaluate
    # 30 trades: 6 @ +2.5%, 24 @ -1%
    # avg_win = 2.5% (above 1.0% structural), hit_rate_floor = 6/30 = 20% (below 35%)
    nets = [0.025] * 6 + [-0.01] * 24
    _seed(_iso_db, nets)
    v = evaluate()
    assert v.avg_win_pct > 0.01
    assert v.pct_hitting_target < 0.35
    assert v.recommendation == "tune"


def test_tier_c_recalibrated():
    from spot_aggro.scoring import TIER_PARAMS
    tc = TIER_PARAMS["C"]
    assert tc.tp_mult == 1.30
    assert tc.sl_mult == 1.40
    assert tc.max_hold_h == 4.0


def test_tier_b_recalibrated():
    from spot_aggro.scoring import TIER_PARAMS
    tb = TIER_PARAMS["B"]
    assert tb.tp_mult == 1.20
    assert tb.sl_mult == 1.20
    assert tb.max_hold_h == 6.0


def test_phase_mm_build_and_flags():
    import re
    from spot_aggro.api.routes import spot_aggro_build, SERVER_BUILD
    m = re.match(r"phase-11n-9-([a-z]+)-2026-04-20$", SERVER_BUILD)
    assert m and (len(m.group(1)), m.group(1)) >= (2, "mm"), SERVER_BUILD
    feats = spot_aggro_build().get("features") or {}
    assert feats.get("target_band_1_5_to_2") is True
    assert feats.get("tier_tp_sl_recalibrated_mm") is True


def test_required_wr_stretch_field_present(_iso_db):
    from spot_aggro.governance.strategy_sufficiency import evaluate
    nets = [0.02, 0.01, -0.005] * 10
    _seed(_iso_db, nets)
    v = evaluate()
    assert hasattr(v, "required_wr_for_stretch")
    assert 0 <= v.required_wr_for_stretch <= 1
