import asyncio
import json

import pytest

from audit import AuditLedger
from binary15m import (
    BinaryInputs,
    Binary15mRunner,
    Mandate,
    PolicyAdapter,
    build_signed_payload,
    canonical_json,
    decide,
    evaluate_promotion,
    sign,
    summarize,
    telemetry_rollup,
    validate_payload,
    verify,
)


def _inp(**o):
    d = dict(
        D5=0.55, D15=0.6, D60=0.55, D240=0.52,
        NewsSent=0.3, SocialSent=0.2, FlowSent=0.2,
        Freshness=0.9, Coverage=0.9,
        Regime=0.3, BaseWR=0.55, Samples=200, ShrinkN=40,
        DecisionPct=68, ConfidencePct=72,
        EventRiskScore=0.1, DriftScore=0.1,
        EntryPx=60_000.0, StopPx=59_500.0, TargetPx=61_000.0,
        FeeR=0.02, SlipR=0.02,
    )
    d.update(o)
    return BinaryInputs(**d)


# ---------- payload signing ----------

def test_sign_verify_round_trip(monkeypatch):
    monkeypatch.setenv("BINARY15M_SIGNING_SECRET", "hunter2")
    p = build_signed_payload(decide(_inp()), strategy_id="binary15m", symbol="BTC/USDT")
    assert "signature" in p
    assert verify(p, p["signature"])


def test_verify_rejects_tampered_payload(monkeypatch):
    monkeypatch.setenv("BINARY15M_SIGNING_SECRET", "hunter2")
    p = build_signed_payload(decide(_inp()), strategy_id="binary15m", symbol="BTC/USDT")
    tampered = dict(p)
    tampered["sizePct"] = (tampered.get("sizePct") or 0) + 1.0
    assert verify(tampered, p["signature"]) is False


def test_verify_rejects_bad_signature(monkeypatch):
    monkeypatch.setenv("BINARY15M_SIGNING_SECRET", "hunter2")
    p = build_signed_payload(decide(_inp()), strategy_id="binary15m")
    assert verify(p, "not-even-base64") is False


def test_sign_requires_secret(monkeypatch):
    monkeypatch.delenv("BINARY15M_SIGNING_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        sign({"a": 1})


def test_canonical_json_sorted_stable():
    a = canonical_json({"b": 1, "a": 2, "signature": "x"})
    b = canonical_json({"a": 2, "b": 1})
    assert a == b


# ---------- payload schema ----------

def test_validate_payload_ok(monkeypatch):
    monkeypatch.setenv("BINARY15M_SIGNING_SECRET", "s")
    p = build_signed_payload(decide(_inp()))
    errors = validate_payload(p)
    assert errors == []


def test_validate_payload_missing_fields():
    errors = validate_payload({"action": "HOLD"})
    assert any("action" in e for e in errors)
    assert any("missing" in e for e in errors)


def test_topcontributors_format(monkeypatch):
    monkeypatch.setenv("BINARY15M_SIGNING_SECRET", "s")
    p = build_signed_payload(decide(_inp()))
    assert isinstance(p["topContributors"], list)
    for c in p["topContributors"]:
        assert set(c.keys()) >= {"feature", "value", "contribution"}


# ---------- explain / reason ----------

def test_summarize_is_human_readable():
    txt = summarize(decide(_inp()))
    assert "BUY" in txt or "SELL" in txt


# ---------- policy adapter ----------

def test_policy_caps_size_per_cycle():
    adapter = PolicyAdapter(Mandate(max_size_pct_per_cycle=0.02))
    d = adapter.apply({"sizePct": 0.10, "forcedFlag": False})
    assert d.size_pct == pytest.approx(0.02)
    assert d.allowed is True


def test_policy_rejects_on_daily_loss_cap():
    adapter = PolicyAdapter(Mandate(max_daily_loss_used_pct=0.03))
    d = adapter.apply({"sizePct": 0.02, "forcedFlag": False},
                      daily_loss_used_pct=0.05)
    assert d.allowed is False
    assert d.rejection_reason == "daily_loss_cap_hit"


def test_policy_rejects_on_exposure_cap():
    adapter = PolicyAdapter(Mandate(max_exposure_pct=0.10))
    d = adapter.apply({"sizePct": 0.02, "forcedFlag": False},
                      current_exposure_pct=0.15)
    assert d.allowed is False
    assert d.rejection_reason == "exposure_cap_hit"


def test_policy_scales_quality_and_forced():
    adapter = PolicyAdapter(Mandate(
        max_size_pct_per_cycle=0.04,
        min_quality_for_full_size=0.8,
        forced_size_factor=0.5,
    ))
    d = adapter.apply({"sizePct": 0.04, "forcedFlag": True}, quality=0.3)
    # forced 0.5 * quality 0.5 = 0.25 → 0.04 * 0.25 = 0.01
    assert d.size_pct == pytest.approx(0.01, rel=0.05)


def test_policy_caps_to_headroom():
    adapter = PolicyAdapter(Mandate(max_exposure_pct=0.05))
    d = adapter.apply({"sizePct": 0.04, "forcedFlag": False},
                      current_exposure_pct=0.03)
    # remaining headroom = 0.02
    assert d.size_pct == pytest.approx(0.02)


# ---------- runner integration (signed + policy) ----------

def test_runner_signs_and_runs_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("BINARY15M_SIGNING_SECRET", "top-secret")
    ledger = AuditLedger(db_path=str(tmp_path / "a.db"), jsonl_path=str(tmp_path / "a.jsonl"))
    async def provider(): return _inp()
    r = Binary15mRunner(provider=provider, ledger=ledger,
                       policy_adapter=PolicyAdapter(Mandate(max_size_pct_per_cycle=0.01)))
    out = asyncio.run(r.tick_once())
    assert out is not None

    rows = ledger.fetch_recent(20)
    kinds = {row["phase"] for row in rows if row.get("kind") == "binary15m"}
    assert "decision" in kinds
    assert "payload" in kinds

    # Pull the signed payload row and verify it
    pay_row = next(r for r in rows if r["phase"] == "payload")
    payload = json.loads(pay_row["result_json"]).get("payload")
    assert payload["action"] in ("BUY", "SELL")
    assert verify(payload, payload["signature"])
    assert payload["sizePct"] <= 0.01
    assert "mandate" in payload["debug"]


# ---------- telemetry / promotion ----------

def test_telemetry_rollup_empty(tmp_path):
    ledger = AuditLedger(db_path=str(tmp_path / "a.db"), jsonl_path=str(tmp_path / "a.jsonl"))
    r = telemetry_rollup(ledger)
    assert r["n"] == 0
    assert r["forced_rate"] == 0.0


def test_telemetry_rollup_with_decisions(tmp_path):
    ledger = AuditLedger(db_path=str(tmp_path / "a.db"), jsonl_path=str(tmp_path / "a.jsonl"))
    for i in range(10):
        ledger.record(kind="binary15m", phase="decision",
                      result={"payload": {"FinalVerdict": "BUY" if i % 2 == 0 else "SELL",
                                          "forcedFlag": i == 0,
                                          "TerminalAction": "PRIMARY",
                                          "PWinPct": 70 + i, "EV_R": 0.2,
                                          "sizePct": 0.02, "ScoreTotal": 75}})
    r = telemetry_rollup(ledger)
    assert r["n"] == 10
    assert r["buy_count"] == 5
    assert r["sell_count"] == 5
    assert r["forced_rate"] == 0.1
    assert r["avg_pwin_pct"] > 70


def test_promotion_evaluate_insufficient_data(tmp_path):
    ledger = AuditLedger(db_path=str(tmp_path / "a.db"), jsonl_path=str(tmp_path / "a.jsonl"))
    ps = evaluate_promotion(ledger=ledger, current_stage="paper")
    assert ps.recommended_next == "same"


def test_promotion_evaluate_demotes_on_high_forced(tmp_path):
    from binary15m.promotion import PromotionThresholds
    ledger = AuditLedger(db_path=str(tmp_path / "a.db"), jsonl_path=str(tmp_path / "a.jsonl"))
    for i in range(50):
        ledger.record(kind="binary15m", phase="decision",
                      result={"payload": {"FinalVerdict": "BUY",
                                          "forcedFlag": True,
                                          "TerminalAction": "DETERMINISTIC"}})
    ps = evaluate_promotion(ledger=ledger, current_stage="canary",
                            thresholds=PromotionThresholds(min_decisions=10))
    assert ps.recommended_next == "demote"
