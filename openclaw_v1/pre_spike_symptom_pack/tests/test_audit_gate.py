"""Audit gate behavioral tests."""
from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import pytest

# Top-level imports for monkeypatch target reuse.
# Function-local relative imports are not reliable across Python versions
# (require __package__ to be set correctly), so we import once at module
# scope and bind a reusable reference. The ag = _audit_gate_mod assignment
# inside each test preserves the original test idiom for monkeypatch.setattr.
from ..services import audit_gate as _audit_gate_mod
from ..services.audit_gate import AuditGate, AuditConfig


@pytest.fixture
def tmp_log_dir(tmp_path):
    d = tmp_path / "audit"
    d.mkdir()
    return d


def _make_intent(strategy_id="pre_spike_symptom_v1", instrument="BTC-USDT",
                 model_version="model_v_test", p_spike=0.7, bar_age_s=10):
    return {
        "id": "test-intent",
        "strategy_id": strategy_id,
        "instrument": instrument,
        "venue": "okx_spot",
        "side": "BUY",
        "size_quote": 100.0,
        "provenance": {
            "model_version": model_version,
            "P_spike": p_spike,
            "bar_close_ts": int(time.time()) - bar_age_s,
            "feature_snapshot": {"atr_14": 1.0, "bbw": 0.05},
            "feature_config": {},
        },
        "audit_token": None,
    }


class _MockArtifactLoader:
    def __init__(self, p_to_return: float):
        self._p = p_to_return
    class _M:
        def __init__(self, p): self.p = p
        def predict(self, _): return self.p
    def load(self, version): return self._M(self._p)


def test_approves_clean_intent(tmp_log_dir, monkeypatch):
    cfg = AuditConfig(
        log_root=str(tmp_log_dir),
        approved_strategies=("pre_spike_symptom_v1",),
        approved_instruments=("BTC-USDT",),
        approved_artifact_versions=("model_v_test",),
    )
    train_stats = {"atr_14": {"mu": 1.0, "sigma": 0.1, "p1": 0.7, "p99": 1.3}}
    # Patch research recompute to return matching features
    ag = _audit_gate_mod  # preserve existing test idiom
    monkeypatch.setattr(
        ag, "compute_features_research",
        lambda bars, feature_config: {"atr_14": 1.0, "bbw": 0.05},
    )
    gate = AuditGate(cfg, train_stats, _MockArtifactLoader(0.7))
    decision = gate.evaluate(_make_intent(), np.zeros((250, 6)))
    assert decision.approved is True
    assert decision.audit_token is not None


def test_rejects_stale_data(tmp_log_dir, monkeypatch):
    cfg = AuditConfig(
        log_root=str(tmp_log_dir), max_bar_age_seconds=60,
        approved_strategies=("pre_spike_symptom_v1",),
        approved_instruments=("BTC-USDT",),
        approved_artifact_versions=("model_v_test",),
    )
    ag = _audit_gate_mod  # preserve existing test idiom
    monkeypatch.setattr(
        ag, "compute_features_research",
        lambda bars, feature_config: {"atr_14": 1.0},
    )
    gate = AuditGate(cfg, {"atr_14": {"mu": 1.0, "sigma": 0.1}},
                     _MockArtifactLoader(0.7))
    decision = gate.evaluate(_make_intent(bar_age_s=600), np.zeros((250, 6)))
    assert decision.approved is False
    assert "stale_data" in decision.failed_checks


def test_rejects_feature_drift(tmp_log_dir, monkeypatch):
    cfg = AuditConfig(
        log_root=str(tmp_log_dir), tolerance_feature_drift=1.0e-6,
        approved_strategies=("pre_spike_symptom_v1",),
        approved_instruments=("BTC-USDT",),
        approved_artifact_versions=("model_v_test",),
    )
    ag = _audit_gate_mod  # preserve existing test idiom
    monkeypatch.setattr(
        ag, "compute_features_research",
        lambda bars, feature_config: {"atr_14": 1.05, "bbw": 0.05},   # drift!
    )
    gate = AuditGate(cfg, {"atr_14": {"mu": 1.0, "sigma": 0.1}},
                     _MockArtifactLoader(0.7))
    decision = gate.evaluate(_make_intent(), np.zeros((250, 6)))
    assert decision.approved is False
    assert "feature_drift" in decision.failed_checks


def test_rejects_policy_violation(tmp_log_dir, monkeypatch):
    cfg = AuditConfig(
        log_root=str(tmp_log_dir),
        approved_strategies=("pre_spike_symptom_v1",),
        approved_instruments=("BTC-USDT",),
        approved_artifact_versions=("model_v_test",),
    )
    ag = _audit_gate_mod  # preserve existing test idiom
    monkeypatch.setattr(ag, "compute_features_research",
                        lambda bars, feature_config: {"atr_14": 1.0})
    gate = AuditGate(cfg, {"atr_14": {"mu": 1.0, "sigma": 0.1}},
                     _MockArtifactLoader(0.7))
    decision = gate.evaluate(_make_intent(instrument="DOGE-USDT"),
                             np.zeros((250, 6)))
    assert decision.approved is False
    assert "policy_violation" in decision.failed_checks


def test_rejects_p_spike_drift(tmp_log_dir, monkeypatch):
    cfg = AuditConfig(
        log_root=str(tmp_log_dir), tolerance_p_spike_drift=1.0e-4,
        approved_strategies=("pre_spike_symptom_v1",),
        approved_instruments=("BTC-USDT",),
        approved_artifact_versions=("model_v_test",),
    )
    ag = _audit_gate_mod  # preserve existing test idiom
    monkeypatch.setattr(ag, "compute_features_research",
                        lambda bars, feature_config: {"atr_14": 1.0})
    # Loader returns a different P than the intent claims
    gate = AuditGate(cfg, {"atr_14": {"mu": 1.0, "sigma": 0.1}},
                     _MockArtifactLoader(0.5))
    decision = gate.evaluate(_make_intent(p_spike=0.7), np.zeros((250, 6)))
    assert decision.approved is False
    assert "model_inference_mismatch" in decision.failed_checks
