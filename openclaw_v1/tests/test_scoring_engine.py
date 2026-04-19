from scoring.engine import ScoreInputs, score


def _good() -> dict:
    return dict(
        NewsSent=0.6, SocialSent=0.4, FlowSent=0.5,
        Freshness=0.9, Coverage=0.9,
        Samples=120, Regime=0.62, ShrinkN=40, BaseWR=0.55,
        D5=0.5, D15=0.5, D60=0.45, D240=0.4,
        DecisionPct=78, ConfidencePct=82,
        EventRiskScore=0.1, DriftScore=0.2,
        EntryPx=100.0, StopPx=99.0, TargetPx=102.5,
        FeeR=0.02, SlipR=0.02,
        VetoReason="", ExecMin=72, WatchMin=58,
    )


def _call(**overrides):
    d = _good()
    d.update(overrides)
    return score(ScoreInputs(**d))


def test_output_shape_has_all_required_fields():
    out = _call()
    required = {
        "RawSent", "SentQuality", "SentimentPct",
        "RawTrend", "AdaptiveTrendPct",
        "MTFConflict", "MTFConfirmPct",
        "CommercialPct", "RiskPenalty", "ConfPlusPct",
        "PWinPct", "RRTrue", "CostR", "EV_R", "EdgePct",
        "BaseScore", "HardPenalty", "ScoreTotal",
        "TerminalAction",
    }
    assert required.issubset(out.keys())


def test_bounds():
    out = _call()
    for k, lo, hi in [
        ("RawSent", -1, 1),
        ("SentQuality", 0, 1),
        ("SentimentPct", 0, 100),
        ("AdaptiveTrendPct", 0, 100),
        ("MTFConfirmPct", 0, 100),
        ("CommercialPct", 0, 100),
        ("ConfPlusPct", 0, 99),
        ("PWinPct", 5, 95),
        ("RRTrue", 0, 6),
        ("EdgePct", 0, 100),
        ("ScoreTotal", 0, 100),
    ]:
        assert lo <= out[k] <= hi, f"{k}={out[k]} out of [{lo},{hi}]"


def test_veto_forces_no_trade_and_zero_confplus():
    out = _call(VetoReason="event_blackout")
    assert out["TerminalAction"] == "NO TRADE"
    assert out["ConfPlusPct"] == 0.0


def test_low_sent_quality_blocks():
    out = _call(Freshness=0.05, Coverage=0.05)
    assert out["SentQuality"] < 0.35
    assert out["TerminalAction"] == "NO TRADE"


def test_zero_stop_distance_blocks():
    out = _call(StopPx=100.0)  # EntryPx=100.0
    assert out["TerminalAction"] == "NO TRADE"


def test_zero_target_distance_blocks():
    out = _call(TargetPx=100.0)
    assert out["TerminalAction"] == "NO TRADE"


def test_negative_ev_blocks():
    # tight target + high cost should force EV negative
    out = _call(
        TargetPx=100.5, StopPx=99.0,   # RR ~ 0.5 — but also triggers RR<1.2 gate
        ConfidencePct=10, DecisionPct=10,
        EventRiskScore=0.9, DriftScore=0.8,
        FeeR=0.5, SlipR=0.5,
    )
    assert out["EV_R"] <= 0
    assert out["TerminalAction"] == "NO TRADE"


def test_bad_conviction_still_blocks_via_pwin_gate():
    # conviction inputs low + risk high → PWinPct below 50 → NO TRADE
    out = _call(ConfidencePct=5, DecisionPct=5, EventRiskScore=0.9, DriftScore=0.8)
    assert out["PWinPct"] < 50.0
    assert out["TerminalAction"] == "NO TRADE"


def test_rr_under_1_2_blocks():
    out = _call(TargetPx=100.8, StopPx=99.0)  # RR ~ 0.8
    assert out["RRTrue"] < 1.20
    assert out["TerminalAction"] == "NO TRADE"


def test_execute_path_reachable():
    out = _call(
        DecisionPct=88, ConfidencePct=90,
        NewsSent=0.8, SocialSent=0.7, FlowSent=0.7,
        Freshness=0.95, Coverage=0.95,
        Samples=400, Regime=0.72, ShrinkN=40, BaseWR=0.55,
        D5=0.7, D15=0.7, D60=0.6, D240=0.55,
        EntryPx=100.0, StopPx=99.0, TargetPx=104.0,
        EventRiskScore=0.05, DriftScore=0.05,
        FeeR=0.01, SlipR=0.01,
    )
    assert out["TerminalAction"] in ("EXECUTE", "WATCH")


def test_hard_penalty_grows_with_event_risk():
    low = _call(EventRiskScore=0.2)
    high = _call(EventRiskScore=0.95)
    assert high["HardPenalty"] > low["HardPenalty"]
    assert high["ScoreTotal"] <= low["ScoreTotal"]


def test_rr_is_geometry_based_not_sentiment_based():
    a = _call(TargetPx=103.0)   # RR=3
    b = _call(TargetPx=102.0)   # RR=2
    assert a["RRTrue"] > b["RRTrue"]
