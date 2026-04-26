"""
Independent audit governance layer.

This service IMPORTS THE RESEARCH PIPELINE'S FEATURE MODULE DIRECTLY.
It does NOT use the strategy's runtime feature module. This is the
bug-isolation property: if runtime drifts, the audit catches it because
it recomputes from the canonical source.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .audit_log import AuditLog, AuditLogEntry, utc_iso_now

# Audit imports the research feature module directly. Strategy runtime imports
# _features_runtime — different module, different code path. Parity is
# enforced by tests/test_features_runtime_research_parity.py.
from ..strategies._features_research import compute_features_research


@dataclass(slots=True)
class AuditConfig:
    log_root: str
    tolerance_feature_drift: float = 1.0e-6
    tolerance_p_spike_drift: float = 1.0e-4
    ood_z_threshold_single: float = 6.0
    ood_z_threshold_pct_features: float = 0.05
    drift_alert_pct: float = 0.60          # rolling approval-rate alert level
    drift_window: int = 1000
    bar_interval_seconds: int = 300        # 5m default
    max_bar_age_seconds: int = 600         # 2x bar interval
    max_clock_skew_seconds: int = 5
    approved_strategies: tuple[str, ...] = ()
    approved_instruments: tuple[str, ...] = ()
    approved_artifact_versions: tuple[str, ...] = ()
    trading_window_blackout_seconds_after_funding: int = 300


@dataclass(slots=True)
class AuditDecision:
    approved: bool
    reasons: list[str]
    failed_checks: list[str]
    audit_token: str | None
    recomputed_p: float
    feature_drift: float
    p_spike_drift: float
    timestamp_iso: str


class AuditGate:
    """Hard gate. Strategies cannot disable it."""

    def __init__(
        self,
        config: AuditConfig,
        train_feature_stats: dict[str, dict[str, float]],
        artifact_loader,
        log: AuditLog | None = None,
    ):
        self.config = config
        self.train_stats = train_feature_stats   # name -> {mu, sigma, p1, p99}
        self.artifact_loader = artifact_loader
        self.log = log or AuditLog(config.log_root)
        self._approval_history: list[bool] = []
        self._long_run_avg: float | None = None

    # ------------------------------------------------------------------
    def evaluate(self, intent: dict, current_window_bars: np.ndarray) -> AuditDecision:
        """current_window_bars: (N, 6) array — t,o,h,l,c,v."""
        t0 = time.perf_counter()
        ts_iso = utc_iso_now()
        prov = intent.get("provenance", {})
        failed: list[str] = []
        reasons: list[str] = []

        # ---------- Policy compliance ----------
        if intent.get("strategy_id") not in self.config.approved_strategies:
            failed.append("policy_violation")
            reasons.append(f"strategy {intent.get('strategy_id')} not whitelisted")
        if intent.get("instrument") not in self.config.approved_instruments:
            failed.append("policy_violation")
            reasons.append(f"instrument {intent.get('instrument')} not whitelisted")
        if prov.get("model_version") not in self.config.approved_artifact_versions:
            failed.append("policy_violation")
            reasons.append(f"model {prov.get('model_version')} not approved")

        # ---------- Freshness ----------
        bar_close_ts = prov.get("bar_close_ts")
        now = int(time.time())
        if bar_close_ts is None:
            failed.append("stale_data"); reasons.append("missing bar_close_ts")
        else:
            age = now - int(bar_close_ts)
            if age < 0:
                # Bar in the future = clock skew or feed bug
                failed.append("stale_data"); reasons.append(f"future_bar_skew={age}s")
            elif age > self.config.max_bar_age_seconds:
                failed.append("stale_data"); reasons.append(f"bar_age={age}s")

        # ---------- Independent recompute ----------
        feature_drift = 0.0
        p_spike_drift = 0.0
        recomputed_p = 0.0
        try:
            recomputed_features = compute_features_research(
                bars=current_window_bars,
                feature_config=prov.get("feature_config", {}),
            )  # returns dict name -> float
            live_features = prov.get("feature_snapshot", {})
            if not live_features:
                failed.append("feature_drift"); reasons.append("missing feature_snapshot")
            else:
                drifts = []
                for k, v_recomp in recomputed_features.items():
                    v_live = live_features.get(k)
                    if v_live is None:
                        failed.append("feature_drift")
                        reasons.append(f"feature {k} missing in live snapshot")
                        continue
                    denom = max(abs(v_live), abs(v_recomp), 1e-12)
                    rel = abs(v_recomp - v_live) / denom
                    drifts.append(rel)
                if drifts:
                    feature_drift = float(max(drifts))
                    if feature_drift > self.config.tolerance_feature_drift:
                        failed.append("feature_drift")
                        reasons.append(f"max_rel_drift={feature_drift:.3e}")

            # ---------- Model verification ----------
            artifact = self.artifact_loader.load(prov.get("model_version"))
            recomputed_p = float(artifact.predict(recomputed_features))
            live_p = float(prov.get("P_spike", 0.0))
            p_spike_drift = abs(recomputed_p - live_p)
            if p_spike_drift > self.config.tolerance_p_spike_drift:
                failed.append("model_inference_mismatch")
                reasons.append(f"p_drift={p_spike_drift:.3e}")

            # ---------- OOD distributional sanity ----------
            ood_count = 0
            ood_total = 0
            for name, val in recomputed_features.items():
                stats = self.train_stats.get(name)
                if stats is None:
                    continue
                mu = stats["mu"]
                sigma = stats["sigma"] if stats["sigma"] > 1e-12 else 1.0
                z = abs((val - mu) / sigma)
                ood_total += 1
                if z > self.config.ood_z_threshold_single:
                    failed.append("out_of_distribution")
                    reasons.append(f"{name} |z|={z:.2f} > single_cap")
                    break
                if z > 3.0:
                    ood_count += 1
            if ood_total > 0 and ood_count / ood_total > self.config.ood_z_threshold_pct_features:
                if "out_of_distribution" not in failed:
                    failed.append("out_of_distribution")
                reasons.append(
                    f"{ood_count}/{ood_total} features |z|>3 "
                    f"({100*ood_count/ood_total:.1f}%)"
                )
        except Exception as e:
            failed.append("recompute_failure")
            reasons.append(f"recompute_exception: {type(e).__name__}: {e}")

        # ---------- Decision ----------
        approved = (len(failed) == 0)
        token = uuid.uuid4().hex if approved else None

        # Track approval rate for drift watchdog
        self._approval_history.append(approved)
        if len(self._approval_history) > self.config.drift_window:
            self._approval_history.pop(0)
        if len(self._approval_history) >= self.config.drift_window:
            recent_rate = sum(self._approval_history) / len(self._approval_history)
            if self._long_run_avg is None:
                self._long_run_avg = recent_rate
            else:
                # EMA over windows
                self._long_run_avg = 0.95 * self._long_run_avg + 0.05 * recent_rate
            if (self._long_run_avg > 0
                    and recent_rate < self.config.drift_alert_pct * self._long_run_avg):
                reasons.append(
                    f"DRIFT_ALERT recent={recent_rate:.2f} "
                    f"vs long_run={self._long_run_avg:.2f}"
                )

        decision = AuditDecision(
            approved=approved,
            reasons=reasons,
            failed_checks=failed,
            audit_token=token,
            recomputed_p=recomputed_p,
            feature_drift=feature_drift,
            p_spike_drift=p_spike_drift,
            timestamp_iso=ts_iso,
        )

        # ---------- Log ----------
        self.log.write(AuditLogEntry(
            ts_iso=ts_iso,
            decision="approved" if approved else "rejected",
            strategy_id=intent.get("strategy_id", "unknown"),
            instrument=intent.get("instrument", "unknown"),
            audit_token=token,
            failed_checks=failed,
            reasons=reasons,
            recomputed_p=recomputed_p,
            feature_drift=feature_drift,
            p_spike_drift=p_spike_drift,
            provenance_summary={
                "model_version": prov.get("model_version"),
                "bar_close_ts": prov.get("bar_close_ts"),
                "P_spike_live": prov.get("P_spike"),
                "audit_latency_us": int((time.perf_counter() - t0) * 1e6),
            },
        ))

        return decision


class ArtifactLoader:
    """Caches model artifacts by version. Verifies SHA-256 of model file."""

    def __init__(self, artifact_root: str):
        self.root = Path(artifact_root)
        self._cache: dict[str, Any] = {}

    def load(self, version: str):
        if version in self._cache:
            return self._cache[version]
        try:
            import lightgbm as lgb
        except ImportError as e:
            raise RuntimeError("lightgbm not installed") from e
        version_dir = self.root / version
        if not version_dir.exists():
            raise FileNotFoundError(f"artifact dir not found: {version_dir}")
        model_path = version_dir / "lightgbm_model.txt"
        with open(version_dir / "manifest.json") as f:
            manifest = json.load(f)
        h = hashlib.sha256()
        with open(model_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        if h.hexdigest() != manifest["sha256_lightgbm_model"]:
            raise RuntimeError(f"artifact integrity check failed: {version}")
        booster = lgb.Booster(model_file=str(model_path))
        feature_order = manifest["feature_order"]

        class _Bound:
            __slots__ = ("booster", "feature_order")
            def __init__(self, b, fo):
                self.booster = b
                self.feature_order = fo
            def predict(self, features: dict[str, float]) -> float:
                row = np.array([[features.get(f, 0.0) for f in self.feature_order]],
                               dtype=np.float32)
                return float(self.booster.predict(row)[0])

        bound = _Bound(booster, feature_order)
        self._cache[version] = bound
        return bound
