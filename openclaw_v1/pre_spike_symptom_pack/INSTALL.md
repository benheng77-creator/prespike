# Installation Guide — pre_spike_symptom_pack

## Prerequisites

- `openclaw_v1/` already runs (orchestrator + BaseStrategy + risk manager
  + OKX perp client all functional).
- Python 3.11+.
- OKX spot API key with `trade` permission, no `withdraw`.
- LightGBM model artifact produced by the discovery pipeline (see
  `research/` directory).

## Step 1 — Drop the folder

```bash
cp -r pre_spike_symptom_pack/. /path/to/openclaw_v1/
```

This adds new files only. No existing file is overwritten.

## Step 2 — Install requirements

```bash
cd /path/to/openclaw_v1/
pip install -r pre_spike_symptom_pack/requirements.txt
```

Speed-critical libraries: `uvloop`, `orjson`, `numpy`, `lightgbm`, `scipy`,
`statsmodels`, `scikit-learn`. All have stdlib fallbacks where possible.

## Step 3 — Apply patches

```bash
cd /path/to/openclaw_v1/
patch -p1 < pre_spike_symptom_pack/patches/orchestrator.patch
patch -p1 < pre_spike_symptom_pack/patches/okx_client_spot.patch
patch -p1 < pre_spike_symptom_pack/patches/risk_manager_spot.patch
patch -p1 < pre_spike_symptom_pack/patches/telemetry_spot.patch
```

Then append the strategy registry block:

```bash
cat pre_spike_symptom_pack/patches/strategies_yaml_append.txt \
    >> config/strategies.yaml
```

## Step 4 — Run the discovery pipeline

```bash
bash pre_spike_symptom_pack/scripts/run_discovery.sh
```

This produces a model artifact in `models/model_v{YYYY-MM-DD}/`. You must
explicitly promote it by editing `config/live.yaml`:

```yaml
artifacts:
  approved_versions:
    - model_v2025-04-19   # add the new version here
  promotion_policy: operator_only
```

## Step 5 — Run tests

```bash
pytest pre_spike_symptom_pack/tests/ -v
```

All tests must pass. `test_features_runtime_research_parity.py` is the
critical one — it verifies that the live runtime feature pipeline
produces bit-for-bit identical output to the research pipeline.

## Step 6 — Paper-trade window

```yaml
# config/live.yaml
spot_trading_enabled: false   # SIGNALS EMIT, NO ORDERS PLACED
```

Run for at least 200 audit decisions. Review the audit log manually:

```bash
python pre_spike_symptom_pack/scripts/verify_audit_chain.py \
    --log-dir /var/openclaw/logs/audit/
```

## Step 7 — Live with 0.25× size

```yaml
spot_trading_enabled: true
spot_caps:
  per_trade_pct: 0.375     # 0.25× the design value of 1.5%
```

Run for 7 calendar days. Review every fill and audit decision daily.

## Step 8 — Ramp to design size

After clean week:

```yaml
spot_caps:
  per_trade_pct: 1.50
```
