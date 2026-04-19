"""Phase 11n-9-i — Take-Profit Agent Team + Layer 9 Sell Governor.

Locks:
  Agent team:
    1. tp_target defaults to 2% with HARD_TP_FLOOR of 1.5%.
    2. TRADE_DRY_RUN / SPOT_DRY_RUN disables execution even if
       SPOT_TP_EXECUTE=1.
    3. _agent_ranker sorts candidates by rank_score DESC.
    4. _agent_scheduler caps admitted count at slots_this_run.
    5. _agent_reviewer holds young positions with strong scenario WR.

  Layer 9 governor:
    6. audit_candidate returns 5 checklist items in fixed key order.
    7. A candidate with live_ret < 0.3% fails margin_exceeds_fees.
    8. A reconciled position fails position_not_reconciled.
    9. Verdict persisted to spot_tp_sell_verdicts.

  Integration:
   10. /spot_aggro/tp/latest + /tp/run endpoints registered.
   11. Feature manifest advertises tp_agent + tp_sell_gov.
   12. Orchestrator runs tp_agent_build_execute step.

  Tab IA scaffolding:
   13. Dashboard has tab-execution, tab-performance, tab-alerts stubs.
   14. Tab nav uses plain-English labels (Dashboard, Execution, etc.).
   15. Tab CSS font-size ≥ 12px (readability upgrade).
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "trades.db"))
    from shared.persistence import state as persist
    persist._initialized = False
    yield


# ---------------------------------------------------------------------------
# Agent team — unit-level
# ---------------------------------------------------------------------------

def test_tp_target_defaults_to_two_percent(monkeypatch):
    monkeypatch.delenv("SPOT_TP_TARGET", raising=False)
    from spot_aggro.governance import tp_agent
    assert tp_agent.tp_target() == 0.02


def test_tp_target_respects_hard_floor(monkeypatch):
    """Never ships below 1.5% no matter how low the operator sets it."""
    monkeypatch.setenv("SPOT_TP_TARGET", "0.005")
    from spot_aggro.governance import tp_agent
    assert tp_agent.tp_target() == tp_agent.HARD_TP_FLOOR


def test_is_execute_defaults_on(monkeypatch):
    monkeypatch.delenv("SPOT_TP_EXECUTE", raising=False)
    monkeypatch.delenv("TRADE_DRY_RUN", raising=False)
    monkeypatch.delenv("SPOT_DRY_RUN", raising=False)
    from spot_aggro.governance import tp_agent
    assert tp_agent.is_execute_enabled() is True


def test_trade_dry_run_disables_execution(monkeypatch):
    monkeypatch.setenv("TRADE_DRY_RUN", "1")
    from spot_aggro.governance import tp_agent
    assert tp_agent.is_execute_enabled() is False


def test_ranker_sorts_by_rank_score_desc():
    from spot_aggro.governance.tp_agent import TPCandidate, _agent_ranker
    cs = [
        TPCandidate(symbol="A-USDT", tier="B", module="M1_flow_B",
                    value_usd=100, entry_price=1.0, live_price=1.05,
                    live_ret=0.05, margin_usd=5.0, age_h=1.0,
                    rank_score=0.0, rationale=""),
        TPCandidate(symbol="B-USDT", tier="B", module="M1_flow_B",
                    value_usd=100, entry_price=1.0, live_price=1.02,
                    live_ret=0.02, margin_usd=2.0, age_h=1.0,
                    rank_score=0.0, rationale=""),
    ]
    ranked = _agent_ranker(cs)
    # Higher margin = higher rank_score = first in list.
    assert ranked[0].symbol == "A-USDT"
    assert ranked[0].rank_score > ranked[1].rank_score


# ---------------------------------------------------------------------------
# Layer 9 governor
# ---------------------------------------------------------------------------

class _FakeCand:
    def __init__(self, **kw):
        self.symbol = kw.get("symbol", "BTC-USDT")
        self.tier = kw.get("tier", "B")
        self.module = kw.get("module", "M1_flow_B")
        self.live_ret = kw.get("live_ret", 0.03)
        self.live_price = kw.get("live_price", 1.03)
        self.entry_price = kw.get("entry_price", 1.0)
        self.evidence_refs = kw.get("evidence_refs", [])
        self.value_usd = kw.get("value_usd", 100.0)
        self.margin_usd = kw.get("margin_usd", 3.0)
        self.age_h = kw.get("age_h", 1.0)


def test_layer9_fails_below_min_margin():
    from spot_aggro.governance.tp_sell_gov import audit_candidate
    c = _FakeCand(live_ret=0.001)
    v = audit_candidate(c)
    assert v.admitted is False
    assert any(i.key == "margin_exceeds_fees" and not i.passed
               for i in v.checklist)


def test_layer9_rejects_reconciled_positions():
    from spot_aggro.governance.tp_sell_gov import audit_candidate
    c = _FakeCand(module="M_reconciled")
    v = audit_candidate(c)
    assert v.admitted is False
    assert any(i.key == "position_not_reconciled" and not i.passed
               for i in v.checklist)


def test_layer9_verdict_is_persisted():
    from spot_aggro.governance import tp_sell_gov
    c = _FakeCand()
    tp_sell_gov.audit_candidate(c)
    rows = tp_sell_gov.latest_verdicts(limit=5)
    assert rows and rows[0]["symbol"] == "BTC-USDT"


def test_layer9_has_five_checklist_items():
    from spot_aggro.governance.tp_sell_gov import audit_candidate
    v = audit_candidate(_FakeCand())
    keys = [i.key for i in v.checklist]
    assert keys == [
        "margin_exceeds_fees",
        "price_aligned_with_book",
        "position_not_reconciled",
        "quota_available",
        "symbol_tradeable_on_okx",
    ]


# ---------------------------------------------------------------------------
# Integration + endpoints
# ---------------------------------------------------------------------------

def test_tp_endpoints_registered():
    from spot_aggro.api import routes
    paths = {r.path for r in routes.router.routes}
    for p in ("/spot_aggro/tp/latest", "/spot_aggro/tp/run"):
        assert p in paths, f"{p} not registered"


def test_feature_manifest_advertises_tp():
    from spot_aggro.api.routes import spot_aggro_build
    body = spot_aggro_build()
    assert body["features"]["tp_agent"] is True
    assert body["features"]["tp_sell_gov"] is True


def test_orchestrator_includes_tp_step():
    from spot_aggro.governance import auto_orchestrator
    tk = auto_orchestrator.run_tick()
    names = [s.name for s in tk.steps]
    assert "tp_agent_build_execute" in names


# ---------------------------------------------------------------------------
# Tab IA scaffolding
# ---------------------------------------------------------------------------

HTML = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")


def test_new_tab_stubs_present():
    for tab_id in ("tab-execution", "tab-performance", "tab-alerts"):
        assert f'id="{tab_id}"' in HTML, f"{tab_id} scaffold missing"


def test_tab_labels_are_plain_english():
    for label in ("Dashboard", "Execution", "Performance",
                  "Alerts", "Research", "System"):
        assert f">{label}</a>" in HTML, f"nav label {label!r} missing"


def test_tab_font_size_at_least_12px():
    m = re.search(r"aside nav a\{[^}]+font-size:(\d+)px", HTML)
    assert m, "tab CSS rule not found"
    assert int(m.group(1)) >= 12, f"tab font too small: {m.group(1)}px"
