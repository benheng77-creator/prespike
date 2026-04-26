# Audit Governance — How the Gate Works and How to Operate It

## Purpose

The audit gate is the LAST check between a strategy emitting a signal and
capital being deployed. Risk manager checks "is the trade size safe."
Audit gate checks "is the SIGNAL CORRECT" — independently.

## What the gate checks

1. **Policy compliance.** Strategy, instrument, and model artifact must
   all be on the explicit whitelist in `config/live.yaml`.
2. **Freshness.** Bar must be < 2× bar interval old. Clock skew between
   bot and exchange ≤ 5s.
3. **Independent feature recompute.** The gate imports the canonical
   research feature module (`strategies/_features_research.py`) — NOT
   the live runtime feature module. It recomputes features from the bar
   window. Any difference > tolerance → `feature_drift` rejection.
4. **Independent model inference.** The gate independently runs the
   model artifact on the recomputed features and compares to the
   strategy's reported P_spike. Mismatch → `model_inference_mismatch`.
5. **Out-of-distribution check.** Each feature's z-score against TRAIN
   distribution. Any |z| > 6 OR more than 5% of features with |z| > 3
   → `out_of_distribution` rejection.
6. **Concept drift watchdog.** Rolling 1000-signal approval rate. Drops
   below 60% of long-run average → alert (does not block current intent).

## Rejection reasons and what to do

- `policy_violation` — A strategy/instrument/model not in the whitelist
  has tried to emit a signal. Fix: review `live.yaml`, OR investigate
  why an unauthorized strategy is wired in.
- `stale_data` — Bar age too high or future bar (clock skew). Fix: check
  data feed health and time sync.
- `feature_drift` — Live feature runtime computed a different value than
  the canonical research module. Fix: this is a CRITICAL FINDING. The
  runtime has drifted from research. Investigate `_features_runtime.py`
  vs `_features_research.py` for the drifting feature.
- `model_inference_mismatch` — The model artifact predicts something
  different from what the strategy reported. Fix: verify model file
  integrity (SHA-256 in manifest), check for accidental in-memory
  artifact swap.
- `out_of_distribution` — Features are far from training distribution.
  Fix: the regime has shifted. Decide whether to (a) halt, (b) retrain,
  or (c) accept higher uncertainty.
- `recompute_failure` — Audit recompute itself crashed. Fix: this is
  always investigated. Audit must NEVER silently fail-open.

## Audit log integrity

Daily files are SHA-256 chained. Every day's file (except the first ever)
contains a header line referencing the prior day's file hash. If any past
file is tampered with, the next day's load detects it.

Verify daily:
```bash
python scripts/verify_audit_chain.py --log-dir /var/openclaw/logs/audit/
```

## Concept drift response

The drift alert is a NOTICE, not a circuit breaker. Steps:

1. Check rolling approval rate over last 1000 signals.
2. Check rejection-reason distribution over last 7 days.
3. If `out_of_distribution` is the dominant reason: market regime has
   shifted. Consider accelerated retraining.
4. If `feature_drift` is the dominant reason: bug in runtime feature
   pipeline. Halt strategy until resolved.
5. If `model_inference_mismatch` is the dominant reason: model file
   corruption or environment drift. Verify SHA-256 of artifact files.
