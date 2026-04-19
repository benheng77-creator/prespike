"""Phase 11n-9-b — Pre-Trade Governor as universal gate.

Layer 8 checklist applies to EVERY trade the engine attempts, not just
Daily Alpha picks. Safety exits (TP/SL/trail/max_hold) bypass the
checklist (record-only, never blocked) so losing positions can't be
stranded.

Locks:
  1. pre_trade_gov.authorize_trade returns a TradeAuthorization with
     passed bool + score + full checklist.
  2. Safety-exit sources (engine_exit:*) always pass with the bypass
     item, regardless of per-symbol stats.
  3. Reconciled-exit sources also bypass.
  4. Engine-entry sources (engine_entry:*) run the full 12-item check.
  5. A symbol with no research data fails with a clear reason.
  6. Every authorize_trade call is persisted; latest() returns newest-first.
  7. Endpoint /spot_aggro/pre_trade/latest registered.
  8. Engine buy path imports + calls pre_trade_gov (source present in
     engine.py at the place_post_only 'buy' sites).
  9. Engine sell path records authorization for safety exits.
 10. Dashboard has pretrade-pill + pretrade-log + autosell-pill +
     autosell-panel DOM slots in c-alpha.
"""
from __future__ import annotations

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


def test_authorize_trade_returns_full_shape():
    from spot_aggro.governance import pre_trade_gov
    a = pre_trade_gov.authorize_trade(
        "BTC-USDT", "buy", "A+", source="manual",
    )
    d = a.to_dict()
    for key in ("authz_id", "ts_ms", "symbol", "side", "tier", "source",
                "passed", "score", "checklist", "target_proj_wr"):
        assert key in d, f"missing {key}"
    assert isinstance(d["checklist"], list)


def test_safety_exit_source_bypasses_and_passes():
    from spot_aggro.governance import pre_trade_gov
    a = pre_trade_gov.authorize_trade(
        "BTC-USDT", "sell", "A+", source="engine_exit:TP",
    )
    assert a.passed is True
    assert a.score == 1.0
    assert any(item["key"] == "safety_exit_bypass" for item in a.checklist)


def test_reconciled_exit_source_bypasses():
    from spot_aggro.governance import pre_trade_gov
    a = pre_trade_gov.authorize_trade(
        "PYTH-USDT", "sell", "RECON", source="engine_reconciled_exit",
    )
    assert a.passed is True


def test_entry_without_research_is_blocked():
    """A buy with no research data = no projected_wr = gov blocks it."""
    from spot_aggro.governance import pre_trade_gov
    a = pre_trade_gov.authorize_trade(
        "UNKNOWN-USDT", "buy", "A+", source="engine_entry:M1_flow",
    )
    assert a.passed is False
    assert a.rejection_reason is not None


def test_authorizations_are_persisted_newest_first():
    from spot_aggro.governance import pre_trade_gov
    a1 = pre_trade_gov.authorize_trade(
        "BTC-USDT", "buy", "A+", source="engine_entry:test",
    )
    time.sleep(0.005)
    a2 = pre_trade_gov.authorize_trade(
        "ETH-USDT", "buy", "A", source="engine_entry:test",
    )
    rows = pre_trade_gov.latest(limit=10)
    assert len(rows) >= 2
    assert rows[0]["authz_id"] == a2.authz_id
    assert rows[1]["authz_id"] == a1.authz_id


def test_pre_trade_endpoint_registered():
    from spot_aggro.api import routes
    paths = {r.path for r in routes.router.routes}
    assert "/spot_aggro/pre_trade/latest" in paths


def test_engine_buy_path_imports_pre_trade_gov():
    src = (REPO / "openclaw_v1" / "spot_aggro" / "engine.py").read_text(
        encoding="utf-8"
    )
    # Both buy sites must invoke the gate.
    assert "from spot_aggro.governance import pre_trade_gov" in src
    assert "pre_trade_gov.authorize_trade(" in src
    # Count: at least 2 places call authorize_trade (normal entry + blitz).
    n = len(re.findall(r"pre_trade_gov\.authorize_trade\(", src))
    assert n >= 2, f"expected ≥2 pre-trade gate calls in engine.py, got {n}"


def test_engine_sell_path_records_authorization():
    src = (REPO / "openclaw_v1" / "spot_aggro" / "engine.py").read_text(
        encoding="utf-8"
    )
    assert 'source=f"engine_exit:{reason}"' in src, (
        "engine sell path must tag authorization with engine_exit:{reason} "
        "so safety exits bypass the gate"
    )


def test_dashboard_has_pretrade_and_autosell_slots():
    html = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    for slot in ("pretrade-pill", "pretrade-log",
                 "autosell-pill", "autosell-panel"):
        assert f'id="{slot}"' in html, f"c-alpha missing slot {slot!r}"


def test_dashboard_alpha_card_is_above_research():
    html = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    # Strip to the in-tab grid.
    tab_start = html.find('id="tab-dash"')
    assert tab_start > 0
    tab_end = html.find("/tab-dash", tab_start)
    grid = html[tab_start:tab_end]
    alpha_pos = grid.find('id="c-alpha"')
    research_pos = grid.find('id="c-research"')
    gov_pos = grid.find('id="c-gov"')
    assert 0 < alpha_pos < research_pos < gov_pos, (
        "c-alpha must sit ABOVE c-research and c-gov in the dashboard grid "
        f"(positions: alpha={alpha_pos}, research={research_pos}, gov={gov_pos})"
    )


def test_research_and_gov_helper_text_advertises_fully_auto():
    html = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    # The research card's helper line should make the full-auto nature
    # explicit so the operator knows they don't need to poke it.
    # Match just the research card region.
    rs_start = html.find('id="c-research"')
    rs_end = html.find('id="c-gov"')
    research_block = html[rs_start:rs_end]
    assert "FULLY AUTO" in research_block
    # And the gov card.
    gov_start = html.find('id="c-gov"')
    gov_end = html.find('id="c-engine"', gov_start)
    if gov_end < 0:
        gov_end = gov_start + 4000
    gov_block = html[gov_start:gov_end]
    assert "FULLY AUTO" in gov_block
