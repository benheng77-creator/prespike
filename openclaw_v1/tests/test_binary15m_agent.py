"""
Three required unit tests per the 15mBinaryAgent blueprint:
    1) Featurizer determinism
    2) Signer roundtrip
    3) Forced resolution on delayed feed → forcedFlag=True + reduced sizePct
"""

import asyncio
import time

import pytest

from binary15m.agent import (
    BinaryAgent,
    EvidenceBundleWriter,
    IngestWorkerStub,
    LocalHmacSigner,
    ModelServer,
    AgentMetrics,
    featurize,
    resolve,
)
from binary15m.agent.model_server import seed_for_bucket
from binary15m.agent.stream_processor import build_cycle_bundle


def _sample_candles(n: int, start_px: float = 60_000.0) -> list[list[float]]:
    out = []
    p = start_px
    for i in range(n):
        p *= 1.0 + 0.0003 * (i % 7 - 3)
        ts = 1_700_000_000_000 + i * 60_000
        out.append([ts, p, p * 1.001, p * 0.999, p, 10.0])
    return out


def _raw_bundle(now_ms: int, *, polymarket_age_ms: int = 0) -> dict:
    """
    Build a realistic raw ingestion dict. The caller controls the
    'polymarket / news_sent' age so we can simulate a delayed feed.
    """
    candles = _sample_candles(260)
    return {
        "candles_15m": {"value": candles, "ts_ms": now_ms - 5_000},
        "candles_1m":  {"value": candles, "ts_ms": now_ms - 3_000},
        "orderbook_top": {
            "value": {"bids": [[60_000, 1.0]], "asks": [[60_010, 1.0]]},
            "ts_ms": now_ms - 500,
        },
        "trades":       {"value": [], "ts_ms": now_ms - 2_000},
        "funding_rate": {"value": 0.0001, "ts_ms": now_ms - 60_000},
        "open_interest":{"value": 1_000_000, "ts_ms": now_ms - 60_000},
        "spread":       {"value": 0.0001, "ts_ms": now_ms - 1_000},
        # This is the "polymarket" / delayed-feed lever:
        "news_sent":    {"value": 0.2, "ts_ms": now_ms - polymarket_age_ms},
        "social_sent":  {"value": 0.1, "ts_ms": now_ms - 120_000},
        "onchain_flow": {"value": 0.0, "ts_ms": now_ms - 90_000},
        "event_risk":   {"value": 0.05, "ts_ms": now_ms - 60_000},
        "drift_score":  {"value": 0.1, "ts_ms": now_ms - 30_000},
    }


# ---------- 1) Featurizer determinism ----------

def test_featurizer_determinism_same_input_same_features():
    ts = 1_700_100_000_000
    raw = _raw_bundle(ts, polymarket_age_ms=60_000)
    bundle1 = build_cycle_bundle(raw, now_ms=ts)
    bundle2 = build_cycle_bundle(raw, now_ms=ts)
    f1 = featurize(bundle1)
    f2 = featurize(bundle2)
    # Deep-equal check: every feature, every nested dict.
    assert f1 == f2, "featurizer must be deterministic"
    # Key invariants
    assert 0.0 <= f1["D5"] <= 1.0
    assert 0.0 <= f1["D15"] <= 1.0
    assert 0.0 <= f1["D60"] <= 1.0
    assert 0.0 <= f1["D240"] <= 1.0
    assert 0.0 <= f1["freshness"] <= 1.0
    assert 0.0 <= f1["coverage"] <= 1.0


# ---------- 2) Signer roundtrip ----------

def test_signer_roundtrip_sign_and_verify(monkeypatch):
    monkeypatch.setenv("BINARY15M_SIGNING_SECRET", "hunter-two-hmac")
    signer = LocalHmacSigner.from_env(key_id="local-hmac-v1")
    payload = {
        "version": "1.0.0",
        "strategyId": "binary15m",
        "cycleId": "abc-123",
        "action": "BUY",
        "entryPx": 60_000.0,
        "stopPx": 59_500.0,
        "targetPx": 61_000.0,
        "sizePct": 0.02,
        "confidencePct": 72.0,
        "p_buy": 0.62,
        "p_sell": 0.38,
        "ev_buy": 0.35,
        "ev_sell": -0.41,
        "topContributors": [{"feature": "D60", "value": 0.6, "contribution": 0.24}],
        "featureVector": {"D60": 0.6},
        "debug": {"modelOutputs": {}, "seed": 42},
        "forcedFlag": False,
        "signatureKeyId": "local-hmac-v1",
    }
    sig = signer.sign(payload)
    assert isinstance(sig, str) and len(sig) > 20
    assert signer.verify(payload, sig) is True

    # Tamper: flipping action invalidates the signature
    tampered = dict(payload); tampered["action"] = "SELL"
    assert signer.verify(tampered, sig) is False

    # Rotate: new key_id but wrong secret rejects
    wrong = LocalHmacSigner(secret="different", key_id="local-hmac-v2")
    assert wrong.verify(payload, sig) is False


# ---------- 3) Forced resolution on delayed feed ----------

def test_forced_resolution_on_delayed_polymarket_feed():
    ts = 1_700_200_000_000
    fresh_raw = _raw_bundle(ts, polymarket_age_ms=60_000)      # fresh enough
    stale_raw = _raw_bundle(ts, polymarket_age_ms=3_600_000)   # 1 hour stale

    def q(raw: dict) -> tuple[dict, dict]:
        b = build_cycle_bundle(raw, now_ms=ts)
        f = featurize(b)
        return b, f

    _, fresh_feat = q(fresh_raw)
    _, stale_feat = q(stale_raw)

    # Stale polymarket forces overall per-feed freshness down.
    assert stale_feat["per_feed_freshness"]["news_sent"] < 0.2
    assert stale_feat["freshness"] < fresh_feat["freshness"]

    # Feed both into the resolver with a neutral model so quality drives forced.
    neutral_model = {"p_buy": 0.51, "p_sell": 0.49}
    seed = seed_for_bucket(ts)

    trace_fresh = resolve(fresh_feat, neutral_model, seed=seed)
    trace_stale = resolve(stale_feat, neutral_model, seed=seed)

    # Fresh at worst hits WATCH (size_factor 0.6), stale should be FORCED.
    assert trace_stale.forced is True
    assert trace_stale.size_factor <= 0.35

    # Forced size factor is strictly smaller than fresh path.
    assert trace_stale.size_factor < trace_fresh.size_factor or trace_fresh.forced

    # Whatever path: always BUY/SELL.
    assert trace_fresh.verdict in ("BUY", "SELL")
    assert trace_stale.verdict in ("BUY", "SELL")


# ---------- no third state ever ----------

def test_no_third_state_ever(monkeypatch):
    """Action must never be PASS / HOLD / WAIT / SKIP / UNKNOWN / NEUTRAL / None."""
    monkeypatch.setenv("BINARY15M_SIGNING_SECRET", "no-third-state")
    from binary15m.agent import validate_signed_decision
    ts = 1_700_500_000_000
    regimes = [
        _raw_bundle(ts, polymarket_age_ms=10),            # fresh
        _raw_bundle(ts, polymarket_age_ms=60_000),        # moderate
        _raw_bundle(ts, polymarket_age_ms=3_600_000),     # stale → forced
        _raw_bundle(ts, polymarket_age_ms=86_400_000),    # very stale
    ]
    for raw in regimes:
        ingest = IngestWorkerStub({
            name: (lambda v=v: v["value"]) for name, v in raw.items()
        })
        agent = BinaryAgent(
            ingest=ingest, model=ModelServer(),
            signer=LocalHmacSigner.from_env(),
            evidence=EvidenceBundleWriter(root="evidence_test"),
            openclaw=None,
        )
        agent.openclaw.post_decision = lambda p: {"sent": False, "reason": "test"}
        out = asyncio.run(agent.run_cycle(now_ms=ts))
        assert out["verdict"] in ("BUY", "SELL"), out["verdict"]
        payload = out["payload"]
        assert payload["action"] in ("BUY", "SELL")
        errors = validate_signed_decision(payload)
        assert errors == [], errors


# ---------- bonus: end-to-end agent cycle with fake ingest ----------

def test_agent_run_cycle_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("BINARY15M_SIGNING_SECRET", "e2e-secret")
    ts = 1_700_300_000_000
    raw = _raw_bundle(ts, polymarket_age_ms=60_000)

    def fake_fetch_once():
        return raw["candles_15m"]["value"]

    # Build an IngestWorkerStub with per-feed fetchers returning scalars/lists.
    ingest = IngestWorkerStub({
        name: (lambda v=v: v["value"]) for name, v in raw.items()
    })
    evidence = EvidenceBundleWriter(root=str(tmp_path / "evidence"))
    agent = BinaryAgent(
        ingest=ingest,
        model=ModelServer(),
        signer=LocalHmacSigner.from_env(),
        evidence=evidence,
        openclaw=None,  # default will fail to post; agent still records evidence
        metrics=AgentMetrics(),
    )
    agent.openclaw.post_decision = lambda payload: {"sent": True, "status": 200, "response": {"ok": True}}
    out = asyncio.run(agent.run_cycle(now_ms=ts))
    assert out["verdict"] in ("BUY", "SELL")
    assert out["payload"]["action"] in ("BUY", "SELL")
    assert out["payload"]["signature"]
    assert out["evidence_path"]
