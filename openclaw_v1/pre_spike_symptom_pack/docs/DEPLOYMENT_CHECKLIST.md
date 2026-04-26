# Deployment Checklist

Run this checklist top-to-bottom for every model promotion.

## Phase 1 — Discovery & training

- [ ] `bash scripts/run_discovery.sh` completed without errors
- [ ] `research/reports/SYMPTOM_DISCOVERY_REPORT.md` reviewed
- [ ] "What we did NOT find" section is non-empty (≥ 5 entries)
- [ ] At least 5 confirmed symptoms in top-20 ranking
- [ ] Holdout PR-AUC lift ≥ 1.5× base rate
- [ ] Model artifact SHA-256 manifest written

## Phase 2 — Tests

- [ ] `pytest tests/` — all green
- [ ] `tests/test_features_runtime_research_parity.py` — drift < tolerance
- [ ] `tests/test_audit_log_chain.py` — chain integrity verified
- [ ] `tests/test_no_lookahead.py` — passes
- [ ] `tests/test_spot_execution.py` — passes

## Phase 3 — Configuration

- [ ] Model version added to `config/live.yaml` `approved_versions`
- [ ] `spot_trading_enabled: false` (paper-only initially)
- [ ] `per_trade_pct: 0.375` (quarter size)
- [ ] `approved_strategies` and `approved_instruments` whitelisted

## Phase 4 — Paper window (≥ 200 audit decisions)

- [ ] Strategy emits signals for at least 1 full trading day
- [ ] Audit decisions reviewed manually
- [ ] Approval rate within 30%-90% (neither rejecting all nor approving all)
- [ ] No `feature_drift` rejections (would indicate live/research mismatch)
- [ ] No `recompute_failure` errors

## Phase 5 — Live with quarter size

- [ ] `spot_trading_enabled: true`, per_trade_pct: 0.375
- [ ] Daily review of every fill for 7 calendar days
- [ ] Daily run of `scripts/verify_audit_chain.py`
- [ ] No unexpected rejections, no spread blowouts, no funding surprises

## Phase 6 — Ramp to design size

- [ ] After clean week, edit `per_trade_pct: 1.50`
- [ ] Hot-reload bot
- [ ] Continue daily reviews for 30 days

## Rollback procedure

If anything in Phases 5–6 goes wrong:
1. `touch /tmp/openclaw.kill` (graceful flatten + halt)
2. Set `spot_trading_enabled: false` in `live.yaml`
3. Investigate audit log for the period in question
4. Determine root cause before resuming
