"""Unit tests for the decision engine scoring pipeline and terminal gate."""

import math

from strategies.decision_engine import DecisionInputs, evaluate


def _base_inputs(**overrides) -> DecisionInputs:
    """Baseline decent-but-not-great inputs; override specific fields per test."""
    defaults = dict(
        NewsSent=0.3, SocialSent=0.4, FlowSent=0.3,
        Freshness=0.9, Coverage=0.9,
        Samples=200.0, Regime=0.60, ShrinkN=50.0, BaseWR=0.5,
        D5=0.5, D15=0.5, D60=0.5, D240=0.5,
        DecisionPct=75.0, ConfidencePct=75.0,
        EventRiskScore=0.2, DriftScore=0.2,
        EntryPx=100.0, StopPx=98.0, TargetPx=106.0,
        FeeR=0.02, SlipR=0.02,
    )
    defaults.update(overrides)
    return DecisionInputs(**defaults)


def test_veto_reason_blocks_trade():
    out = evaluate(_base_inputs(VetoReason="event_freeze"))
    assert out.TerminalAction == "NO TRADE"
    assert out.ConfPlusPct == 0.0


def test_low_sent_quality_blocks_trade():
    out = evaluate(_base_inputs(Freshness=0.1, Coverage=0.1))
    assert out.SentQuality < 0.35
    assert out.TerminalAction == "NO TRADE"


def test_zero_stop_distance_blocks_trade():
    out = evaluate(_base_inputs(StopPx=100.0))
    assert out.TerminalAction == "NO TRADE"


def test_zero_target_distance_blocks_trade():
    out = evaluate(_base_inputs(TargetPx=100.0))
    assert out.TerminalAction == "NO TRADE"


def test_rr_below_threshold_blocks_trade():
    # Target 1% away, stop 2% away → RR = 0.5
    out = evaluate(_base_inputs(EntryPx=100.0, StopPx=98.0, TargetPx=101.0))
    assert out.RRTrue < 1.20
    assert out.TerminalAction == "NO TRADE"


def test_negative_ev_blocks_trade():
    out = evaluate(_base_inputs(FeeR=5.0, SlipR=5.0))
    assert out.EV_R <= 0
    assert out.TerminalAction == "NO TRADE"


def test_all_conditions_met_executes():
    out = evaluate(_base_inputs(
        DecisionPct=92.0, ConfidencePct=92.0,
        D5=0.9, D15=0.9, D60=0.9, D240=0.9,
        EntryPx=100.0, StopPx=98.0, TargetPx=110.0,  # RR = 5.0
    ))
    assert out.TerminalAction == "EXECUTE"
    assert out.EV_R >= 0.25
    assert out.CommercialPct >= 68.0
    assert out.ConfPlusPct >= 66.0


def test_pwin_bounded_at_extremes():
    high = evaluate(_base_inputs(
        DecisionPct=100.0, ConfidencePct=100.0,
        D5=1.0, D15=1.0, D60=1.0, D240=1.0,
        EventRiskScore=0.0, DriftScore=0.0,
    ))
    low = evaluate(_base_inputs(
        DecisionPct=0.0, ConfidencePct=0.0,
        D5=-1.0, D15=-1.0, D60=-1.0, D240=-1.0,
        EventRiskScore=1.0, DriftScore=1.0,
    ))
    assert 5.0 <= high.PWinPct <= 95.0
    assert 5.0 <= low.PWinPct <= 95.0


def test_score_bounded():
    out = evaluate(_base_inputs(
        DecisionPct=0.0, ConfidencePct=0.0,
        D5=-1.0, D15=-1.0, D60=-1.0, D240=-1.0,
        EventRiskScore=1.0, DriftScore=1.0,
    ))
    assert 0.0 <= out.ScoreTotal <= 100.0


def test_rr_true_computed_from_geometry():
    out = evaluate(_base_inputs(EntryPx=100.0, StopPx=95.0, TargetPx=115.0))
    # |115 - 100| / |100 - 95| = 3.0
    assert math.isclose(out.RRTrue, 3.0, rel_tol=1e-9)


def test_rr_true_clipped_at_six():
    out = evaluate(_base_inputs(EntryPx=100.0, StopPx=99.9, TargetPx=200.0))
    assert out.RRTrue == 6.0


def test_sent_quality_is_geometric_mean():
    out = evaluate(_base_inputs(Freshness=0.64, Coverage=0.81))
    # sqrt(0.64 * 0.81) = 0.72
    assert math.isclose(out.SentQuality, 0.72, rel_tol=1e-3)


def test_mtf_conflict_penalizes_score():
    conflicted = evaluate(_base_inputs(D5=1.0, D15=-1.0, D60=1.0, D240=-1.0))
    aligned = evaluate(_base_inputs(D5=0.5, D15=0.5, D60=0.5, D240=0.5))
    assert conflicted.MTFConfirmPct < aligned.MTFConfirmPct


def test_edgepct_bounded():
    out = evaluate(_base_inputs())
    assert 0.0 <= out.EdgePct <= 100.0


def test_edgepct_rewards_positive_ev():
    bad = evaluate(_base_inputs(FeeR=1.0, SlipR=1.0))
    good = evaluate(_base_inputs(
        DecisionPct=92.0, ConfidencePct=92.0,
        EntryPx=100.0, StopPx=98.0, TargetPx=110.0,
    ))
    assert good.EdgePct > bad.EdgePct
