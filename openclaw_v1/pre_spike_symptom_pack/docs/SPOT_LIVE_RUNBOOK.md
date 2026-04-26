# Spot Live Runbook

## Pre-deployment checklist

- [ ] Discovery pipeline ran clean
- [ ] Model artifact created and SHA-256 manifest written
- [ ] Holdout PR-AUC lift ≥ 1.5× base rate
- [ ] All tests pass: `pytest tests/`
- [ ] `_features_runtime` vs `_features_research` parity test passes
- [ ] Model version added to `config/live.yaml` `approved_versions`
- [ ] OKX spot API key has `trade` permission, NO `withdraw`
- [ ] Operating capital deposited and visible in `spot_get_balance`
- [ ] Audit log directory exists and is writable
- [ ] At least 200 paper-trade audit decisions reviewed manually

## Going live with quarter size

```yaml
# config/live.yaml
spot_trading_enabled: true
spot_caps:
  per_trade_pct: 0.375     # 0.25× design
```

Run for 7 calendar days. Review every fill and every audit decision daily.

## Ramping to design size (after clean week)

```yaml
spot_caps:
  per_trade_pct: 1.50
```

## Emergency flatten

```python
from services.spot_execution import SpotExecutionService
n = await spot_exec.flatten_all()
```

Or via kill-switch:
```bash
touch /tmp/openclaw.kill
```

## Rolling back to a previous model

1. Edit `config/live.yaml`:
   ```yaml
   strategies:
     pre_spike_symptom_v1:
       params:
         active_model_version: model_v_PREVIOUS
   ```
2. Confirm `model_v_PREVIOUS` is in `approved_versions`.
3. Hot-reload: `kill -HUP $(pgrep -f openclaw)`.
4. Verify new model loaded by checking startup log line.

## Reading the daily audit log

```bash
cat /var/openclaw/logs/audit/$(date -u +%F).jsonl | \
    jq 'select(.decision == "rejected") | .failed_checks'
```

Report key counts to the operator dashboard daily.
