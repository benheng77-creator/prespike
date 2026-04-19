"""
Upstream conviction model.

Trains a classifier on the raw feature values observed at each historical
trade-open event, with the target being whether the trade was a winner
(`pnl_r > 0`). The model's output — P(win) in [0, 1] — is multiplied by
100 and used to replace the hardcoded `DecisionPct` and `ConfidencePct`
stubs in the decision engine.

Two model backends are supported:

    "logreg" — sklearn LogisticRegression (default; fast, linear, interpretable)
    "gbm"    — sklearn GradientBoostingClassifier (nonlinear, catches
               interactions, slower to fit)

Features fed into the model (15 total):
    D5, D15, D60, D240             — multi-timeframe directional scores
    DriftScore                     — short vs long return distribution shift
    Samples, Regime                — Bayesian trend inputs
    NewsSent, SocialSent, FlowSent — sentiment sources (0 in backtest)
    Freshness, Coverage            — sentiment/flow quality
    EventRiskScore                 — economic calendar proximity
    ATR_pct                        — risk / entry price (volatility scale)
    RR                             — |target-entry| / |entry-stop|

Training flow:
    1. Run backtest to accumulate trades with full DecisionInputs stored
    2. Extract feature vectors and binary labels from each trade
    3. Time-ordered 70/30 split, standardize, fit the configured model
    4. Report train + holdout log loss / AUC
    5. Pickle (scaler, model, model_type) to disk
"""

from __future__ import annotations

import logging
import os
import pickle
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler

log = logging.getLogger(__name__)

CONVICTION_FEATURE_NAMES = [
    "D5", "D15", "D60", "D240",
    "DriftScore",
    "Samples", "Regime",
    "NewsSent", "SocialSent", "FlowSent",
    "Freshness", "Coverage",
    "EventRiskScore",
    "ATR_pct", "RR",
]

MODEL_TYPES = ("logreg", "gbm")


def build_feature_vector(
    d5: float,
    d15: float,
    d60: float,
    d240: float,
    drift: float,
    samples: float,
    regime: float,
    news_sent: float,
    social_sent: float,
    flow_sent: float,
    freshness: float,
    coverage: float,
    event_risk: float,
    atr_pct: float,
    rr: float,
) -> list:
    return [
        d5, d15, d60, d240,
        drift,
        samples, regime,
        news_sent, social_sent, flow_sent,
        freshness, coverage,
        event_risk,
        atr_pct, rr,
    ]


@dataclass
class ConvictionFitResult:
    n_trades: int
    n_wins: int
    n_train: int
    n_holdout: int
    train_log_loss: float
    train_auc: float
    holdout_log_loss: Optional[float]
    holdout_auc: Optional[float]
    model_type: str


class ConvictionModel:
    def __init__(self, model_type: str = "logreg") -> None:
        if model_type not in MODEL_TYPES:
            raise ValueError(
                f"model_type must be one of {MODEL_TYPES}, got {model_type!r}"
            )
        self.model_type = model_type
        self.scaler: Optional[StandardScaler] = None
        self.model = None

    def _make_model(self):
        if self.model_type == "gbm":
            return GradientBoostingClassifier(
                n_estimators=100,
                max_depth=3,
                learning_rate=0.05,
                random_state=42,
            )
        return LogisticRegression(max_iter=1000)

    def _features_from_trade(self, trade) -> Optional[list]:
        inp = getattr(trade, "inputs", None)
        if inp is None:
            return None
        risk = abs(inp.EntryPx - inp.StopPx)
        reward = abs(inp.TargetPx - inp.EntryPx)
        rr = reward / risk if risk > 0 else 0.0
        atr_pct = risk / inp.EntryPx if inp.EntryPx > 0 else 0.0
        return build_feature_vector(
            inp.D5, inp.D15, inp.D60, inp.D240,
            inp.DriftScore,
            inp.Samples, inp.Regime,
            inp.NewsSent, inp.SocialSent, inp.FlowSent,
            inp.Freshness, inp.Coverage,
            inp.EventRiskScore,
            atr_pct, rr,
        )

    def fit(self, trades: List, holdout_pct: float = 0.3) -> ConvictionFitResult:
        rows = []
        labels = []
        for t in trades:
            feats = self._features_from_trade(t)
            if feats is None:
                continue
            rows.append(feats)
            labels.append(1 if t.pnl_r > 0 else 0)

        if len(rows) < 20:
            raise ValueError(
                f"Need at least 20 trades with inputs attached, got {len(rows)}"
            )

        X = np.array(rows, dtype=float)
        y = np.array(labels, dtype=int)

        if len(set(y)) < 2:
            raise ValueError(
                f"All {len(y)} trades have the same outcome — "
                "cannot train a single-class classifier."
            )

        split = int(len(X) * (1 - holdout_pct))
        X_train, X_test = X[:split], X[split:]
        y_train, y_test = y[:split], y[split:]

        if len(set(y_train)) < 2:
            raise ValueError(
                "Training split has single class — shuffle or extend data"
            )

        self.scaler = StandardScaler()
        X_train_std = self.scaler.fit_transform(X_train)

        self.model = self._make_model()
        self.model.fit(X_train_std, y_train)

        train_probs = self.model.predict_proba(X_train_std)[:, 1]
        train_ll = float(log_loss(y_train, np.clip(train_probs, 1e-6, 1 - 1e-6)))
        train_auc = (
            float(roc_auc_score(y_train, train_probs))
            if len(set(y_train)) == 2
            else 0.5
        )

        holdout_ll: Optional[float] = None
        holdout_auc: Optional[float] = None
        if len(y_test) > 0 and len(set(y_test)) >= 2:
            X_test_std = self.scaler.transform(X_test)
            test_probs = self.model.predict_proba(X_test_std)[:, 1]
            holdout_ll = float(log_loss(y_test, np.clip(test_probs, 1e-6, 1 - 1e-6)))
            holdout_auc = float(roc_auc_score(y_test, test_probs))

        return ConvictionFitResult(
            n_trades=len(rows),
            n_wins=int(y.sum()),
            n_train=len(y_train),
            n_holdout=len(y_test),
            train_log_loss=train_ll,
            train_auc=train_auc,
            holdout_log_loss=holdout_ll,
            holdout_auc=holdout_auc,
            model_type=self.model_type,
        )

    def predict(self, feature_vector: list) -> float:
        if self.scaler is None or self.model is None:
            return 0.5
        X = np.array([feature_vector], dtype=float)
        X_std = self.scaler.transform(X)
        return float(self.model.predict_proba(X_std)[0, 1])

    def save(self, path: str) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(
                {
                    "scaler": self.scaler,
                    "model": self.model,
                    "model_type": self.model_type,
                },
                f,
            )

    def load(self, path: str) -> bool:
        if not os.path.exists(path):
            return False
        with open(path, "rb") as f:
            state = pickle.load(f)
        self.scaler = state["scaler"]
        self.model = state["model"]
        self.model_type = state.get("model_type", "logreg")
        return True
