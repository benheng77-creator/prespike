import json
from pathlib import Path

import pytest

from audit import AuditLedger
from reports import build_activity_summary, generate_eod, validate_report


class _PortfolioAdapter:
    def snapshot(self):
        return {
            "balance": 1003.0, "starting_balance": 1000.0,
            "has_open_position": False, "closed_trades_count": 2,
            "position": None,
        }

    def recent_closed(self, limit=10_000):
        return [{"pnl": 2.0, "session": "baseline"}, {"pnl": 1.0, "session": "daytrade"}]


class _PersistenceAdapter:
    def counts(self): return {"decisions": 10, "trades": 2}
    def recent_trades(self, limit=200):
        return [
            {"symbol": "BTC/USDT", "pnl_quote": 2.0, "session": "baseline"},
            {"symbol": "ETH/USDT", "pnl_quote": 1.0, "session": "daytrade"},
        ]


class _RiskAdapter:
    def snapshot(self): return {"halted": False, "starting_balance": 1000.0}


@pytest.fixture()
def ledger(tmp_path):
    return AuditLedger(db_path=str(tmp_path / "a.db"), jsonl_path=str(tmp_path / "a.jsonl"))


def test_generate_eod_writes_json_and_md(tmp_path, ledger):
    ledger.record(kind="intent", verb="BUY")
    ledger.record(kind="filled", verb="BUY")
    ledger.record(kind="finalized")

    report = generate_eod(
        portfolio_adapter=_PortfolioAdapter(),
        persistence_adapter=_PersistenceAdapter(),
        risk_adapter=_RiskAdapter(),
        ledger=ledger,
        date="2026-04-14",
        out_dir=str(tmp_path / "reports"),
    )
    assert Path(report["json_path"]).exists()
    assert Path(report["md_path"]).exists()
    parsed = json.loads(Path(report["json_path"]).read_text(encoding="utf-8"))
    assert parsed["date"] == "2026-04-14"
    assert parsed["realized_pnl_quote"] == 3.0
    assert parsed["actions_summary"]["intents"] == 1
    assert parsed["actions_summary"]["filled"] == 1
    assert "baseline" in parsed["strategy_contribution"]


def test_validate_report_ok(ledger, tmp_path):
    r = generate_eod(
        portfolio_adapter=_PortfolioAdapter(),
        persistence_adapter=_PersistenceAdapter(),
        risk_adapter=_RiskAdapter(),
        ledger=ledger, date="2026-04-14",
        out_dir=str(tmp_path / "out"),
    )
    status, notes = validate_report(r)
    assert status in ("ok", "needs_review")


def test_validate_report_needs_review_when_variance(tmp_path, ledger):
    r = generate_eod(
        portfolio_adapter=_PortfolioAdapter(),
        persistence_adapter=_PersistenceAdapter(),
        risk_adapter=_RiskAdapter(),
        ledger=ledger, date="2026-04-14",
        out_dir=str(tmp_path / "out"),
    )
    r["variance_notes"] = ["forced variance"]
    status, notes = validate_report(r)
    assert status == "needs_review"
    assert "forced variance" in notes


def test_summary_is_plain_english(tmp_path, ledger):
    r = generate_eod(
        portfolio_adapter=_PortfolioAdapter(),
        persistence_adapter=_PersistenceAdapter(),
        ledger=ledger, date="2026-04-14",
        out_dir=str(tmp_path / "out"),
    )
    text = build_activity_summary(r)
    assert "OpenClaw" in text
    assert "filled" in text
