# pre_spike_symptom_pack — Drop-in Integration

Speed-first, production-grade pre-spike symptom strategy with OKX spot
auto-execution and independent audit governance, designed to drop straight
into an existing `openclaw_v1/` tree.

## Speed Posture

- **NumPy-native incremental computation.** No per-bar pandas DataFrames in
  hot paths. All rolling windows are pre-allocated `np.ndarray` ring buffers.
- **`uvloop` event loop** when available (falls back cleanly).
- **`ujson` / `orjson`** for hot-path JSON; stdlib `json` fallback.
- **Pre-compiled feature pipeline.** Feature spec is parsed once at startup
  into a flat list of (name, fn, args). Per-bar update is a tight loop over
  function pointers, not a dict lookup.
- **LightGBM `Booster.predict`** directly on a `np.ndarray` row — no DataFrame
  round-trip.
- **Hot-path zero allocation.** Rolling stats reuse pre-allocated buffers.
  GC pressure measured and tested.
- **Audit recompute runs OFF the hot path** in a worker — the live signal
  is emitted with provenance; audit recomputes asynchronously and either
  approves (attaches token) or rejects (drops intent before execution).

Median per-bar latency target: **< 5 ms** end-to-end on the live runtime
(feature update → prediction → veto checks → intent assembly).

## Folder Layout

Drop the entire `pre_spike_symptom_pack/` folder INTO your `openclaw_v1/`
root. Files merge cleanly with the existing tree (no overwrites). Manual
merges required:

- `core/orchestrator.py` — apply the patch in `patches/orchestrator.patch`
  to insert the audit gate hook (one location).
- `services/okx_client.py` — apply `patches/okx_client_spot.patch` to
  add spot endpoint methods (additive, no changes to perp methods).
- `services/risk_manager.py` — apply `patches/risk_manager_spot.patch`.
- `services/telemetry.py` — apply `patches/telemetry_spot.patch`.
- `config/strategies.yaml` — append the YAML block in
  `patches/strategies_yaml_append.txt`.

All other files are NEW — drop them in as-is.

## Files

```
pre_spike_symptom_pack/
├── README.md
├── INSTALL.md
├── requirements.txt
├── services/
│   ├── audit_gate.py                  # NEW — independent governance layer
│   ├── audit_log.py                   # NEW — SHA-256 chained append-only log
│   ├── spot_execution.py              # NEW — spot order + soft-stop manager
│   └── live_persistence.py            # NEW — append-only signal log
├── strategies/
│   ├── pre_spike_symptom_v1.py        # NEW — strategy (panel + BaseStrategy)
│   ├── _symptom_engine.py             # NEW — model + feature runtime
│   ├── _features_runtime.py           # NEW — incremental feature compute
│   └── _features_research.py          # NEW — canonical feature module
│                                      #        (used by audit recompute)
├── research/
│   ├── ingest_history.py              # offline: 3y bar download + clean
│   ├── label_spikes.py                # offline: forward-window labeling
│   ├── compute_features.py            # offline: full feature matrix
│   ├── discover_symptoms.py           # offline: 3-pass discovery
│   ├── train_model.py                 # offline: LightGBM + isotonic calib
│   └── evaluate_holdout.py            # offline: single-shot holdout
├── config/
│   ├── live.yaml                      # spot caps + audit config
│   └── pre_spike_symptom.yaml         # strategy params
├── patches/
│   ├── orchestrator.patch
│   ├── okx_client_spot.patch
│   ├── risk_manager_spot.patch
│   ├── telemetry_spot.patch
│   └── strategies_yaml_append.txt
├── tests/
│   ├── test_audit_gate.py
│   ├── test_spot_execution.py
│   ├── test_pre_spike_symptom_strategy.py
│   ├── test_features_runtime_research_parity.py
│   ├── test_no_lookahead.py
│   └── test_audit_log_chain.py
├── docs/
│   ├── AUDIT_GOVERNANCE.md
│   ├── SPOT_LIVE_RUNBOOK.md
│   ├── KNOWN_LIMITATIONS.md
│   └── DEPLOYMENT_CHECKLIST.md
└── scripts/
    ├── run_discovery.sh
    └── verify_audit_chain.py
```

## Critical Operational Notes

1. **Audit gate is ALWAYS on.** Strategies cannot disable it. The orchestrator
   patch hardcodes the gate hook.
2. **Auto-promotion of model artifacts is FORBIDDEN.** Operator must edit
   `config/live.yaml`'s `approved_versions` list manually.
3. **Spot has no native stop on OKX.** Soft stops are bot-managed. The 24/7
   resilience tests are mandatory before live capital.
4. **First 7 days of live trading: 0.25× size.** See `DEPLOYMENT_CHECKLIST.md`.
5. **Audit log is append-only and SHA-256 chained.** Tamper-evident. Run
   `scripts/verify_audit_chain.py` daily.

See `INSTALL.md` for step-by-step integration.
