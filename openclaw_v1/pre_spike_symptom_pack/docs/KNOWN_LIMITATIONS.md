# Known Limitations

These are the real limits of the system. Acknowledge them; do not pretend
they don't exist.

## Audit gate caveats

- The audit gate is only as good as the recomputation pipeline. If both the
  runtime AND the canonical research module share a bug, the gate cannot
  catch it. Mitigation: research module is the same code that produced the
  training data, so any drift between them is also detectable as a parity
  failure in `tests/test_features_runtime_research_parity.py` — this test
  must be in CI.
- Concept drift detection is a LAGGING indicator (rolling 1000 signals).
  Hard regime shifts may produce losses before the alert fires. The alert
  is a NOTICE, not a circuit breaker.

## Spot-specific caveats

- OKX spot has no native stop-loss ticket. Soft stops are bot-managed.
  This means the 24/7 resilience tests are mandatory: a crashed bot with
  open spot positions will not auto-stop on adverse moves.
- Spot fees on OKX are higher than perp maker. Reflect this in size and
  frequency expectations.
- Spread / liquidity on the lower-cap pairs (e.g. SOL-USDT) can be lumpy
  during off-hours. The strategy's spread veto handles the obvious cases
  but extreme dislocation may still produce non-ideal fills.
- Maker-only execution can miss fast moves. Acceptable trade-off in v1;
  taker fallback is intentionally NOT enabled.

## Model caveats

- Trained on 3-year window. Future regimes may invalidate the model. The
  weekly retrain cron mitigates this, but does not eliminate the risk.
- Direction-agnostic spike prediction is inherently lower-edge than
  direction-specific. Expect modest, not spectacular, returns.
- Base rate is low (~5% of bars). Even a strong PR-AUC produces many
  false positives in absolute count. The audit gate's OOD check is the
  main filter against pathological false positives.

## Operational caveats

- Auto-promotion of model artifacts is FORBIDDEN. Operator must explicitly
  edit `config/live.yaml`. This is a feature, not a bug.
- The 7-day quarter-size ramp is mandatory. Skipping it is the single most
  common way live-strategy deployments produce surprise losses.
