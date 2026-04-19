import asyncio
import math

import pytest

from audit import AuditLedger
from binary15m import (
    BinaryConfig,
    BinaryInputs,
    Binary15mRunner,
    DEFAULT_CONFIG,
    OUTPUT_FIELDS,
    decide,
    kelly_size_pct,
)


def _base(**over) -> BinaryInputs:
    d = dict(
        D5=0.55, D15=0.58, D60=0.56, D240=0.52,
        NewsSent=0.4, SocialSent=0.3, FlowSent=0.2,
        Freshness=0.9, Coverage=0.9,
        Regime=0.3, BaseWR=0.55, Samples=150, ShrinkN=40,
        DecisionPct=70, ConfidencePct=72,
        EventRiskScore=0.1, DriftScore=0.15,
        EntryPx=60_000.0, StopPx=59_500.0, TargetPx=61_000.0,
        FeeR=0.02, SlipR=0.02,
    )
    d.update(over)
    return BinaryInputs(**d)


# ------- contract -------

def test_output_has_every_required_field():
    out = decide(_base())
    missing = set(OUTPUT_FIELDS) - set(out.keys())
    assert not missing, f"missing: {missing}"


def test_final_verdict_is_always_buy_or_sell():
    # Sweep extreme and neutral inputs
    cases = [
        _base(),
        _base(D5=0.5, D15=0.5, D60=0.5, D240=0.5,
              NewsSent=0, SocialSent=0, FlowSent=0, Regime=0, BaseWR=0.5,
              DecisionPct=50, ConfidencePct=50),
        _base(D5=0.01, D15=0.01, D60=0.01, D240=0.01,
              EventRiskScore=0.9, DriftScore=0.9),
        _base(D5=0.99, D15=0.99, D60=0.99, D240=0.99,
              EventRiskScore=0.9, DriftScore=0.9),
        _base(NewsSent=-0.9, SocialSent=-0.9, FlowSent=-0.9,
              D5=0.4, D15=0.4, D60=0.4, D240=0.45),
    ]
    for c in cases:
        out = decide(c)
        assert out["FinalVerdict"] in ("BUY", "SELL"), out["FinalVerdict"]


# ------- formula exactness -------

def test_signed_timeframe_conversion():
    out = decide(_base(D5=0.8, D15=0.6, D60=0.5, D240=0.3))
    assert out["d5"]   == pytest.approx(2 * (0.8 - 0.5))
    assert out["d15"]  == pytest.approx(2 * (0.6 - 0.5))
    assert out["d60"]  == pytest.approx(0.0)
    assert out["d240"] == pytest.approx(2 * (0.3 - 0.5))


def test_q_sent_formula():
    out = decide(_base(Freshness=0.8, Coverage=0.8))
    assert out["q_sent"] == pytest.approx(math.sqrt(0.64))


def test_q_trend_formula():
    out = decide(_base(Samples=180, ShrinkN=20))
    assert out["q_trend"] == pytest.approx(math.sqrt(180/(180+20)))


def test_conflict_and_qmtf():
    o = decide(_base(D5=0.6, D15=0.5, D60=0.4, D240=0.5))
    # d5=0.2 d15=0 d60=-0.2 d240=0 → conflict=(.2+.2+.2)/6=0.1
    assert o["Conflict"] == pytest.approx(0.1)
    assert o["q_mtf"] == pytest.approx(math.exp(-0.3))


def test_sent_raw_and_alpha_sent_clips():
    o = decide(_base(NewsSent=1, SocialSent=1, FlowSent=1, Freshness=1, Coverage=1))
    # raw = 1, q=1, alpha = clip(1*1, -.22, .22) = .22
    assert o["SentRaw"] == pytest.approx(1.0)
    assert o["alpha_sent"] == pytest.approx(0.22)


def test_rr_true_clipped_to_6():
    o = decide(_base(EntryPx=100, StopPx=99, TargetPx=1_000))
    assert o["RRTrue"] == 6.0


def test_ev_and_edge_pct_bounds():
    o = decide(_base())
    assert 0.0 <= o["EdgePct"] <= 100.0
    assert 0.0 <= o["ScoreTotal"] <= 100.0


def test_direction_follows_dir_score_sign():
    bull = decide(_base(D5=0.9, D15=0.9, D60=0.85, D240=0.8,
                        NewsSent=0.9, SocialSent=0.9, FlowSent=0.9,
                        Regime=0.8, BaseWR=0.7))
    bear = decide(_base(D5=0.1, D15=0.1, D60=0.15, D240=0.2,
                        NewsSent=-0.9, SocialSent=-0.9, FlowSent=-0.9,
                        Regime=-0.8, BaseWR=0.3))
    assert bull["Direction"] == "BUY"
    assert bear["Direction"] == "SELL"


def test_buy_structure_pass_rules():
    o = decide(_base(D5=0.9, D15=0.6, D60=0.6, D240=0.5))
    assert o["BuyStructurePass"] is True
    assert o["SellStructurePass"] is False


def test_sell_structure_pass_rules():
    o = decide(_base(D5=0.1, D15=0.4, D60=0.4, D240=0.5))
    assert o["SellStructurePass"] is True
    assert o["BuyStructurePass"] is False


# ------- gating paths -------

def test_primary_path_reachable_under_ideal_conditions():
    o = decide(_base(
        D5=0.9, D15=0.9, D60=0.9, D240=0.9,
        NewsSent=0.9, SocialSent=0.9, FlowSent=0.9,
        Regime=0.8, BaseWR=0.7, Samples=400, ShrinkN=40,
        DecisionPct=90, ConfidencePct=90,
        EventRiskScore=0.05, DriftScore=0.05,
        EntryPx=60_000, StopPx=59_500, TargetPx=62_000,
        FeeR=0.01, SlipR=0.01,
    ))
    assert o["TerminalAction"] == "PRIMARY"
    assert o["forcedFlag"] is False
    assert o["FinalVerdict"] == "BUY"


def test_deterministic_path_fires_when_nothing_passes():
    # extreme uncertainty + bad structure
    o = decide(_base(
        D5=0.51, D15=0.49, D60=0.51, D240=0.49,
        NewsSent=0, SocialSent=0, FlowSent=0,
        Freshness=0.2, Coverage=0.2,
        Regime=0, BaseWR=0.5, Samples=10, ShrinkN=40,
        DecisionPct=50, ConfidencePct=50,
        EventRiskScore=0.8, DriftScore=0.8,
        EntryPx=60_000, StopPx=59_500, TargetPx=60_100,  # tiny RR
        FeeR=0.05, SlipR=0.05,
    ))
    assert o["TerminalAction"] == "DETERMINISTIC"
    assert o["forcedFlag"] is True
    assert o["FinalVerdict"] in ("BUY", "SELL")
    assert o["forcedReason"] and len(o["forcedReason"]) > 0


def test_deterministic_compressions_applied_on_high_risk():
    o = decide(_base(
        D5=0.52, D15=0.52, D60=0.52, D240=0.52,
        EventRiskScore=0.85, DriftScore=0.70,
        # force deterministic path
        Freshness=0.1, Coverage=0.1,
        EntryPx=60_000, StopPx=59_500, TargetPx=60_050,
        FeeR=0.05, SlipR=0.05,
    ))
    assert o["TerminalAction"] == "DETERMINISTIC"
    assert o["debug"]["modelOutputs"]["comp_mult"] < 1.0


def test_size_pct_within_cap():
    out = decide(_base())
    assert 0.0 <= out["sizePct"] <= DEFAULT_CONFIG.maxPositionPct


def test_pwin_pct_bounds():
    for _ in range(10):
        o = decide(_base())
        assert 50.0 <= o["PWinPct"] <= 98.0


def test_primary_requires_structure_match():
    # High scores but Direction points BUY while SellStructure holds → no primary
    o = decide(_base(
        # make DirScore positive (BUY) via sentiment, but timeframes say SELL
        D5=0.2, D15=0.3, D60=0.4, D240=0.45,
        NewsSent=1, SocialSent=1, FlowSent=1,
        Regime=1, BaseWR=0.9,
        EntryPx=60_000, StopPx=59_500, TargetPx=62_500,
        FeeR=0.01, SlipR=0.01,
    ))
    assert o["TerminalAction"] in ("WATCH", "DETERMINISTIC")


# ------- sizing -------

def test_kelly_size_zero_when_edge_negative():
    # p=0.3, RR=1 → k = 0.3 - 0.7 = -0.4 → clipped to 0
    assert kelly_size_pct(30.0, 1.0, risk_factor=0.5, max_position_pct=0.05) == 0.0


def test_kelly_size_caps_at_max():
    # Very high edge — should be capped.
    k = kelly_size_pct(95.0, 4.0, risk_factor=1.0, max_position_pct=0.05)
    assert k == pytest.approx(0.05)


# ------- runner -------

@pytest.mark.asyncio
async def _unused():  # placeholder; asyncio used directly below to avoid plugin dep
    pass


def test_runner_tick_calls_provider_and_records(tmp_path):
    ledger = AuditLedger(db_path=str(tmp_path / "a.db"), jsonl_path=str(tmp_path / "a.jsonl"))

    calls = {"n": 0}

    async def provider():
        calls["n"] += 1
        return _base()

    received = []
    async def on_verdict(o):
        received.append(o["FinalVerdict"])

    r = Binary15mRunner(provider=provider, ledger=ledger, on_verdict=on_verdict)

    out = asyncio.run(r.tick_once())
    assert out is not None
    assert out["FinalVerdict"] in ("BUY", "SELL")
    assert calls["n"] == 1
    assert received and received[0] in ("BUY", "SELL")

    rows = ledger.fetch_recent(5)
    assert any(r["kind"] == "binary15m" and r["phase"] == "decision" for r in rows)


def test_runner_skipped_when_provider_returns_none(tmp_path):
    ledger = AuditLedger(db_path=str(tmp_path / "a.db"), jsonl_path=str(tmp_path / "a.jsonl"))
    async def provider(): return None
    r = Binary15mRunner(provider=provider, ledger=ledger)
    out = asyncio.run(r.tick_once())
    assert out is None
    rows = ledger.fetch_recent(5)
    assert any(r["kind"] == "binary15m" and r["phase"] == "skipped" for r in rows)
