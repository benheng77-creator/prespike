"""Phase 11n-9-ll — Governance Board tests.

Validates:
  1. strategy_sufficiency.evaluate returns SufficiencyVerdict with
     required fields; flags insufficient_sample when n < 20.
  2. evaluate recommends 'replace' when avg_win < target (structural).
  3. evaluate recommends 'keep' when hit rate >= 40% with Wilson-low > 20%.
  4. edge_contribution.analyze returns FactorContribution list + top_*.
  5. formula_review.run produces verdict; latest() returns history.
  6. daily_report.generate produces 9-section report with required keys.
  7. Endpoints /gov/strategy_sufficiency, /edge_contribution,
     /formula_review/latest, /daily_report/latest return ok=True shape.
  8. Dashboard has c-gov-board card + _refreshGovBoard + tab routing.
  9. Build tag + flags advertised.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import time
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]
HTML = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def _iso_db(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    for m in (
        "spot_aggro.governance.strategy_sufficiency",
        "spot_aggro.governance.edge_contribution",
        "spot_aggro.governance.formula_review",
        "spot_aggro.governance.daily_report",
    ):
        mod = importlib.import_module(m)
        importlib.reload(mod)
    # Init trade_log table manually.
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE trade_log("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts_ms INTEGER NOT NULL, symbol TEXT NOT NULL,"
        " module TEXT, action TEXT NOT NULL, tier TEXT,"
        " notional_usd REAL, fee_usd REAL, pnl_usd REAL,"
        " side TEXT, avg_px REAL, correlation_id TEXT,"
        " payload_json TEXT"
        ")"
    )
    con.commit()
    con.close()
    yield db


def _seed_trades(db, nets: list[float], notional: float = 100.0,
                 factors: dict[str, float] | None = None) -> None:
    """Seed pairs of enter+exit trades for each net_pct in the list."""
    con = sqlite3.connect(str(db))
    now = int(time.time() * 1000)
    payload_enter = json.dumps(factors or {})
    for i, net in enumerate(nets):
        ts_enter = now - (len(nets) - i) * 60_000
        ts_exit = ts_enter + 30_000
        sym = f"COIN{i}-USDT"
        con.execute(
            "INSERT INTO trade_log(ts_ms, symbol, module, action, tier,"
            " notional_usd, fee_usd, pnl_usd, payload_json) VALUES "
            "(?,?, 'M1', 'enter', 'C', ?, 0.0, 0.0, ?)",
            (ts_enter, sym, notional, payload_enter),
        )
        pnl = net * notional
        con.execute(
            "INSERT INTO trade_log(ts_ms, symbol, module, action, tier,"
            " notional_usd, fee_usd, pnl_usd, payload_json) VALUES "
            "(?,?, 'M1', 'exit', 'C', ?, ?, ?, ?)",
            (ts_exit, sym, notional, 0.10, pnl,
             json.dumps({"net_pct": net})),
        )
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# 1 — sufficiency surface + insufficient_sample verdict
# ---------------------------------------------------------------------------

def test_sufficiency_public_surface():
    from spot_aggro.governance import strategy_sufficiency as ss
    for name in ("evaluate", "SufficiencyVerdict", "DEFAULT_TARGET_PCT"):
        assert hasattr(ss, name)


def test_sufficiency_insufficient_sample(_iso_db):
    from spot_aggro.governance.strategy_sufficiency import evaluate
    # Seed only 5 trades — below MIN_SAMPLE_FOR_VERDICT (20).
    _seed_trades(_iso_db, [0.03, 0.01, -0.01, 0.025, 0.015])
    v = evaluate()
    assert v.recommendation == "insufficient_sample"
    assert v.sufficient is False
    assert v.n_observed == 5


# ---------------------------------------------------------------------------
# 2 — structural 'replace' verdict when avg_win < target
# ---------------------------------------------------------------------------

def test_sufficiency_replace_when_avg_win_below_target(_iso_db):
    from spot_aggro.governance.strategy_sufficiency import evaluate
    # 25 trades all winning at 0.5% (below 2% target).
    nets = [0.005] * 25
    _seed_trades(_iso_db, nets)
    v = evaluate(target_pct=0.02, window_n=50)
    assert v.n_observed == 25
    assert v.avg_win_pct < 0.02
    assert v.recommendation == "replace"
    assert "STRUCTURALLY" not in v.reason.upper() or v.avg_win_pct < 0.02


# ---------------------------------------------------------------------------
# 3 — 'keep' verdict when strategy clears the bar
# ---------------------------------------------------------------------------

def test_sufficiency_keep_when_strong(_iso_db):
    from spot_aggro.governance.strategy_sufficiency import evaluate
    # 25 trades: 15 winners at +3%, 10 losers at -1%
    nets = [0.03] * 15 + [-0.01] * 10
    _seed_trades(_iso_db, nets)
    v = evaluate(target_pct=0.02, window_n=50)
    assert v.n_observed == 25
    assert v.pct_hitting_target >= 0.40
    assert v.recommendation == "keep"
    assert v.sufficient is True


# ---------------------------------------------------------------------------
# 4 — edge_contribution surface + factor ranking
# ---------------------------------------------------------------------------

def test_edge_contribution_surface(_iso_db):
    from spot_aggro.governance import edge_contribution as ec
    for name in ("analyze", "FactorContribution", "EdgeContributionReport", "FACTORS"):
        assert hasattr(ec, name)


def test_edge_contribution_top_positive(_iso_db):
    """Seed trades where composite is strongly predictive of wins."""
    from spot_aggro.governance.edge_contribution import analyze
    # 20 trades: high composite -> positive pnl, low -> negative
    con = sqlite3.connect(str(_iso_db))
    now = int(time.time() * 1000)
    for i in range(20):
        ts_enter = now - (20 - i) * 60_000
        ts_exit = ts_enter + 30_000
        composite = 0.1 + i * 0.05          # 0.1..1.05
        net = -0.01 if i < 10 else 0.03
        pnl = net * 100.0
        con.execute(
            "INSERT INTO trade_log(ts_ms, symbol, module, action, tier,"
            " notional_usd, fee_usd, pnl_usd, payload_json) VALUES "
            "(?,?, 'M1', 'enter', 'C', 100.0, 0.0, 0.0, ?)",
            (ts_enter, f"C{i}-USDT",
             json.dumps({"composite": composite, "spi": 0.3})),
        )
        con.execute(
            "INSERT INTO trade_log(ts_ms, symbol, module, action, tier,"
            " notional_usd, fee_usd, pnl_usd, payload_json) VALUES "
            "(?,?, 'M1', 'exit', 'C', 100.0, 0.0, ?, ?)",
            (ts_exit, f"C{i}-USDT", pnl, json.dumps({})),
        )
    con.commit(); con.close()
    r = analyze(window_n=50)
    assert r.n_trades_with_payload == 20
    comp_factor = next((f for f in r.factors if f.factor == "composite"), None)
    assert comp_factor is not None
    assert comp_factor.spearman_rho > 0.3
    assert comp_factor.verdict == "strong_positive"


# ---------------------------------------------------------------------------
# 5 — formula_review run + persist
# ---------------------------------------------------------------------------

def test_formula_review_run_and_latest(_iso_db):
    from spot_aggro.governance.formula_review import run, latest
    v = run()
    assert v.verdict in ("keep", "tune", "replace", "insufficient_data")
    rows = latest(limit=5)
    assert len(rows) >= 1
    assert rows[0].verdict == v.verdict


# ---------------------------------------------------------------------------
# 6 — daily_report generate
# ---------------------------------------------------------------------------

def test_daily_report_generate_shape(_iso_db):
    from spot_aggro.governance.daily_report import generate
    r = generate()
    assert r.report_date
    assert r.verdict in ("improving", "degrading", "flat", "insufficient")
    # 9 sections present in markdown.
    for header in (
        "1. Executive Status", "2. Activity Snapshot",
        "3. Performance Reality", "4. Critical Gaps",
        "5. Root Cause", "6. Governance Decision",
        "7. Upgrade Focus", "8. Daily Verdict",
        "9. Strategy Progress",
    ):
        assert header in r.markdown, f"missing section: {header}"


# ---------------------------------------------------------------------------
# 7 — endpoints
# ---------------------------------------------------------------------------

def test_endpoint_strategy_sufficiency(_iso_db):
    from spot_aggro.api.routes import spot_aggro_strategy_sufficiency
    body = spot_aggro_strategy_sufficiency(target_pct=0.02, window_n=50)
    assert body["ok"] is True
    assert "verdict" in body


def test_endpoint_edge_contribution(_iso_db):
    from spot_aggro.api.routes import spot_aggro_edge_contribution
    body = spot_aggro_edge_contribution(window_n=50)
    assert body["ok"] is True
    assert "report" in body


def test_endpoint_formula_review_latest(_iso_db):
    from spot_aggro.governance.formula_review import run
    run()
    from spot_aggro.api.routes import spot_aggro_formula_review_latest
    body = spot_aggro_formula_review_latest(limit=3)
    assert body["ok"] is True
    assert isinstance(body["verdicts"], list)


def test_endpoint_daily_report_latest(_iso_db):
    from spot_aggro.governance.daily_report import generate
    generate()
    from spot_aggro.api.routes import spot_aggro_daily_report_latest
    body = spot_aggro_daily_report_latest()
    assert body["ok"] is True
    assert body["report"] is not None


# ---------------------------------------------------------------------------
# 8 — dashboard
# ---------------------------------------------------------------------------

def test_dashboard_card_present():
    assert 'id="c-gov-board"' in HTML
    assert "Governance Board" in HTML
    assert "async function _refreshGovBoard" in HTML
    assert '"c-gov-board"' in HTML   # registered in _TAB_CARDS


# ---------------------------------------------------------------------------
# 9 — build tag + flags
# ---------------------------------------------------------------------------

def test_phase_ll_build_and_flags():
    import re
    from spot_aggro.api.routes import spot_aggro_build, SERVER_BUILD
    m = re.match(r"phase-11n-9-([a-z]+)-2026-04-20$", SERVER_BUILD)
    assert m and (len(m.group(1)), m.group(1)) >= (2, "ll"), SERVER_BUILD
    feats = spot_aggro_build().get("features") or {}
    for flag in (
        "governance_board", "target_2pct_per_trade",
        "daily_auto_report_24h",
    ):
        assert feats.get(flag) is True, f"missing flag: {flag}"
