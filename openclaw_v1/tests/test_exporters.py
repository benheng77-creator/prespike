import json
import os
from pathlib import Path

import pytest

from audit import AuditLedger
from reports import generate_eod
from reports.exporters import (
    export_to_cli,
    export_to_dashboard,
    export_to_file,
    export_to_telegram,
)


class _PortfolioAdapter:
    def snapshot(self):
        return {
            "balance": 1003.0, "starting_balance": 1000.0,
            "has_open_position": False, "closed_trades_count": 2,
            "position": None,
        }

    def recent_closed(self, limit=10_000):
        return [{"pnl": 2.0, "session": "baseline"}, {"pnl": 1.0, "session": "baseline"}]


@pytest.fixture()
def report(tmp_path):
    ledger = AuditLedger(db_path=str(tmp_path / "a.db"), jsonl_path=str(tmp_path / "a.jsonl"))
    ledger.record(kind="intent", verb="BUY")
    ledger.record(kind="filled", verb="BUY")
    return generate_eod(
        portfolio_adapter=_PortfolioAdapter(),
        ledger=ledger, date="2026-04-14",
        out_dir=str(tmp_path / "reports"),
    )


def test_cli_exporter_prints_and_returns(report, capsys):
    out = export_to_cli(report)
    assert "EOD" in out
    assert "Realized" in out


def test_file_exporter_verifies_paths(report):
    s = export_to_file(report)
    assert s["json_ok"] is True
    assert s["md_ok"] is True


def test_dashboard_exporter_writes_json(tmp_path, report):
    path = tmp_path / "latest.json"
    p = export_to_dashboard(report, cache_path=str(path))
    assert Path(p).exists()
    data = json.loads(Path(p).read_text(encoding="utf-8"))
    assert data["date"] == "2026-04-14"
    assert "summary" in data


def test_telegram_exporter_noop_without_env(monkeypatch, report):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    r = export_to_telegram(report)
    assert r == {"sent": False, "reason": "not_configured"}
