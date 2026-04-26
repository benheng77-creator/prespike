"""
SymptomEngine — owns model loading, feature runtime, prediction, provenance.

Used by both the panel-contract `generate_signal` and the BaseStrategy class.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from ._features_runtime import FeatureRuntime


@dataclass(slots=True)
class ModelArtifact:
    version: str
    booster: object               # lightgbm.Booster
    feature_order: list[str]
    feature_normalizer: dict      # name -> {mu, sigma}
    threshold_tau: float
    train_feature_stats: dict     # for OOD reporting (audit owns hard check)


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_artifact(artifact_root: str, version: str) -> ModelArtifact:
    import lightgbm as lgb
    root = Path(artifact_root) / version
    with open(root / "manifest.json") as f:
        manifest = json.load(f)
    expected = manifest["sha256_lightgbm_model"]
    actual = _sha256_of(root / "lightgbm_model.txt")
    if expected != actual:
        raise RuntimeError(f"artifact integrity mismatch for {version}")
    booster = lgb.Booster(model_file=str(root / "lightgbm_model.txt"))
    with open(root / "feature_normalizer.json") as f:
        norm = json.load(f)
    with open(root / "threshold_tau.json") as f:
        tau = json.load(f)["tau"]
    train_stats = manifest.get("train_feature_stats", {})
    return ModelArtifact(
        version=version, booster=booster,
        feature_order=manifest["feature_order"],
        feature_normalizer=norm, threshold_tau=float(tau),
        train_feature_stats=train_stats,
    )


class SymptomEngine:
    def __init__(self, artifact_root: str, active_version: str,
                 default_threshold_offset: float = 0.0):
        self.artifact = load_artifact(artifact_root, active_version)
        self.runtime = FeatureRuntime(self.artifact.feature_order)
        self.threshold_offset = default_threshold_offset
        self._mu = np.array(
            [self.artifact.feature_normalizer.get(f, {"mu": 0.0})["mu"]
             for f in self.artifact.feature_order], dtype=np.float64)
        self._sigma = np.array(
            [max(self.artifact.feature_normalizer.get(f, {"sigma": 1.0})["sigma"], 1e-12)
             for f in self.artifact.feature_order], dtype=np.float64)
        self._last_features: dict[str, float] = {}
        self._last_atr: float = 0.0

    @property
    def threshold_tau(self) -> float:
        return self.artifact.threshold_tau + self.threshold_offset

    def update(self, ts: int, o: float, h: float, l: float, c: float, v: float):
        self.runtime.update(ts, o, h, l, c, v)

    def is_warm(self) -> bool:
        return self.runtime.is_warm()

    def predict(self) -> tuple[float, dict[str, float]]:
        """Returns (P_spike, feature_snapshot)."""
        raw = self.runtime.vector
        if not np.all(np.isfinite(raw)):
            return math.nan, {}
        # z-score
        z = (raw - self._mu) / self._sigma
        # LightGBM expects 2D
        p = float(self.artifact.booster.predict(z.reshape(1, -1))[0])
        # snapshot — RAW (not normalized) for audit recompute parity
        snapshot = {f: float(raw[i]) for i, f in enumerate(self.artifact.feature_order)}
        self._last_features = snapshot
        for k in ("atr_14",):
            if k in snapshot and math.isfinite(snapshot[k]):
                self._last_atr = snapshot[k]
        return p, snapshot

    @property
    def last_atr(self) -> float:
        return self._last_atr
