# MIGRATION_AUDIT.md — Phase 1 (SPOT AGGRO only)

**Status:** Phase 1 audit — NO code changes made. Awaiting operator acknowledgment before Phase 2.
**Scope rule (non-negotiable):** SPOT AGGRO and APEX OMEGA are separate engines. This audit covers
`openclaw_v1/spot_aggro/**`, its required `shared/**` deps, and `deploy/spot-aggro-v2/**` ONLY.
**Out of scope (do not touch):** `openclaw_v1/apex_omega/**`, `openclaw_v1/claw/**`, anything under
`core/`, `audit/`, `backtest_plus/`, `features/`, `monitors/`, `scheduler/`, `strategies/`,
`scoring/` (root), and any test under `openclaw_v1/tests/` that is not spot-labelled. Those are a
separate engine's surface area and will be audited separately if/when that engine is in scope.
**Frozen module (do not re-architect):** `openclaw_v1/spot_aggro/forensic_v2/**` — user directive,
forensic-truth module is already upgraded for this task.

---

## 0. METHOD

1. Traced live imports from `openclaw_v1/server.py` → `apex_omega/api/routes.py` → spot endpoints
   (the only entry points that touch spot_aggro in production).
2. Traced live imports inside `openclaw_v1/spot_aggro/**` to identify every module the live engine
   actually loads. `shared/**` is the only external namespace that spot_aggro imports; zero imports
   of `apex_omega.*`, `claw.*`, `core.*`, or any other package. Separation confirmed on the spot side.
3. Classified every file touched by the spot live path as KEEP / REWRITE / DELETE, with the reason
   tied to one of the 5 gates (L0–L4) or a hard rule (R1–R12).

---

## 1. SEPARATION FINDINGS (ground truth before any edits)

- `openclaw_v1/spot_aggro/**` imports only from `shared.*` and from its own subpackages. **No cross
  imports from `apex_omega`.** Clean on the spot side.
- `openclaw_v1/apex_omega/**` contains near-duplicate copies of `adapters/`, `config/`,
  `notifications/`, `persistence/`, `llm/` that also exist under `shared/`. `shared/` is the
  canonical copy the spot engine uses; the apex_omega copies are the other engine's private shim.
  **Both copies are intentionally preserved by the separation rule — do not "unify" them.**
- `openclaw_v1/spot_aggro/forensic_v2/schema.py:283` and `apex_omega/api/routes.py:648` import
  `spot_aggro.swarm.runner.get_swarm_state`. That is one-directional (apex routes read spot state
  via the shared API layer). Fine per current separation, but L4/L2 rebuild must not introduce the
  reverse direction.
- `shared/adapters/universe_discovery.py` is a **perp-instrument** scanner (`instType=SWAP`, line
  159) imported by `spot_aggro/engine.py:267` as a fallback when `cfg.universe` is empty. This is
  R11 violation territory: a spot-only engine pulling a perp universe. Flagged below.

---

## 2. LIVE SPOT CODE PATH (what production actually loads)

Entry: HTTP request → `openclaw_v1/server.py` → mounts `apex_omega.api.routes` router → spot
endpoints call into spot_aggro.

Files actually executed on the spot live path:

    openclaw_v1/server.py                 # FastAPI shim (spot endpoints mounted via apex router)
    openclaw_v1/apex_omega/api/routes.py  # HOST for spot endpoints — mixed-tenant (see note)
    openclaw_v1/spot_aggro/__init__.py
    openclaw_v1/spot_aggro/engine.py
    openclaw_v1/spot_aggro/scoring.py
    openclaw_v1/spot_aggro/spi.py
    openclaw_v1/spot_aggro/prompts.py
    openclaw_v1/spot_aggro/coin_memory.py
    openclaw_v1/spot_aggro/swarm/runner.py
    openclaw_v1/spot_aggro/swarm/models.py
    openclaw_v1/spot_aggro/swarm/integration.py
    openclaw_v1/spot_aggro/swarm/prompts.py
    openclaw_v1/spot_aggro/research/runner.py
    openclaw_v1/spot_aggro/research/mio.py
    openclaw_v1/spot_aggro/research/prompts.py
    openclaw_v1/spot_aggro/forensic_v2/**        # FROZEN — not in this audit
    openclaw_v1/shared/adapters/okx_unified.py
    openclaw_v1/shared/adapters/universe_discovery.py  # R11 risk (perp scanner on spot)
    openclaw_v1/shared/config/__init__.py + spot_aggro_config.yml
    openclaw_v1/shared/llm/consensus.py
    openclaw_v1/shared/llm/clients/*.py
    openclaw_v1/shared/notifications/router.py
    openclaw_v1/shared/persistence/state.py
    openclaw_v1/shared/persistence/settings.py
    deploy/spot-aggro-v2/index.html + build.sh
    web/ops/index.html (dashboard)

Note on `apex_omega/api/routes.py`: it **hosts** spot endpoints (`/apex/spot_aggro/*`) but is an
apex_omega file. Per separation rule, spot routes must be moved to a spot-owned router in a later
phase. Flagged as REWRITE/RELOCATE but not touched in Phase 1.

---

## 3. INVENTORY — DELETE

Files that the 5-gate contract makes physically unreachable or that violate R1–R12.

### 3.1 Tier C entry surface (R7, L3 gate — Tier C DISABLED at startup)

| File | Lines | Reason |
|---|---|---|
| `openclaw_v1/spot_aggro/scoring.py` (Tier "C" branch only) | 59–72, 74, 191, and any `TIER_PARAMS["C"]` / `TIER_DAILY_CAPS["C"]` usage | R7: Tier C DISABLED at startup until >=50 shadow trades prove positive expectancy. Hard-remove the "C" entry in `TIER_PARAMS`, the `"C": 40` cap in `TIER_DAILY_CAPS`, and the `classify_tier` branch that returns `"C"`. Shadow-logging goes through a separate calibration path in Phase 5, not via the live tier enum. |
| `openclaw_v1/spot_aggro/engine.py` C-tier code paths | search `"C"` tier refs in `_classify_universe`, `_scan_tiered`, `_size_for_tier`, `_exit_for_tier` | Same as above. DELETE branches; do not keep behind a flag (spec forbids toggles). |

(File-level deletion not possible here because `scoring.py` and `engine.py` mix A/B/C — the branches
are deleted in Phase 2/3 under the "no Tier C" rule. Listed here so the inventory is complete.)

### 3.2 Perp-scanner path on a spot-only account (R11)

| File | Reason |
|---|---|
| Import `shared.adapters.universe_discovery` at `spot_aggro/engine.py:267` + fallback block 267–269 | R11: this fetches OKX `instType=SWAP` (perps). Spot engine must not probe perps. DELETE the fallback; require `cfg.universe` to be populated with spot symbols at boot. If empty → L0 refuses to start. |
| `shared/adapters/universe_discovery.py` (as a **spot** dependency) | Not deleted from disk — the file may still be used by apex_omega. Spot must stop importing it. If and only if nothing in `openclaw_v1/**` (excluding apex_omega) still imports it, a follow-up spot-only audit can remove its spot-side pycache; the file itself is apex-shared and out of scope. |

### 3.3 Legacy forensic generator (superseded by frozen forensic_v2)

| File | Lines | Reason |
|---|---|---|
| `openclaw_v1/spot_aggro/forensic/runner.py` | 347 | Pre-forensic_v2 narrative generator. Produces reports without the PROVEN/LIKELY/WEAK/UNVERIFIABLE confidence propagation required by L4. Replaced by `forensic_v2/**` (frozen). Still referenced by `apex_omega/api/routes.py:771,781,797`. DELETE this file and the 3 route handlers that call it. |
| `openclaw_v1/spot_aggro/forensic/models.py`, `forensic/pdf_renderer.py`, `forensic/prompts.py` | — | Same rationale — all are v1 narrative stack. DELETE with runner.py. |
| `openclaw_v1/spot_aggro/forensic/reports/*.pdf` (untracked artifacts in git status) | — | Stale v1 outputs. DELETE from working tree; `.gitignore` already excludes them so no repo change. |

### 3.4 Swarm with posterior-0.500 / 3-of-5 race (Q5, Q1 violations)

| File | Lines | Reason |
|---|---|---|
| `openclaw_v1/spot_aggro/swarm/runner.py` | 432 | Current ensemble emits `buy_confidence=0.5` as a real verdict when agents fail to respond (Q5). Entry uses 3+/5 as quorum (Q1 violation). DELETE AND REPLACE in Phase 7 (audit-swarm rebuild). Data model in `swarm/models.py` is reusable (see REWRITE). |
| `openclaw_v1/spot_aggro/swarm/prompts.py` | — | Legacy prompts for the 5-role "trade swarm" (quant/structure/liquidity/regime/adjudicator). The new roles are truth-auditor/physics/state/calibration/chief-adjudicator. DELETE; rewrite in Phase 7 with new roles. |
| `openclaw_v1/spot_aggro/swarm/integration.py` | 132 | Blends `ci.buy_confidence` into composite (`swarm_composite_adjustment`) and treats absent data as `1.0`/`0.0` (fail-open). Violates R6 (no-quorum = REJECT, not neutral) and L3 (score blend replaces the calibration table). DELETE; new integration will be a hard gate in Phase 7, not a weighted blend. |

### 3.5 Composite-score-as-function (L3 violation)

| File | Lines | Reason |
|---|---|---|
| `openclaw_v1/spot_aggro/scoring.py::compute_composite_score` + callers | — | L3 requires `score → action` be a **lookup table** of VALID buckets, not a live function. The current composite is a weighted function that fires regardless of realized expectancy. DELETE the function-as-gate usage; keep the raw component features (they feed the calibration table). Enforced in Phase 5. |

### 3.6 Dashboard Tier-C counter (R7)

| File | Surface | Reason |
|---|---|---|
| `web/ops/index.html` | any "Tier C: N/40" tile, Tier-C row in matrix | R7: Tier C disabled — counter must not exist. DELETE the tile and row in Phase 10. |

---

## 4. INVENTORY — KEEP (unchanged by Phase 1)

These files are correctly scoped spot-only, do not violate any gate as-is, and must not be edited
in the upgrade unless a later phase explicitly requires it.

| File | Lines | Why keep |
|---|---|---|
| `openclaw_v1/spot_aggro/__init__.py` | — | Package marker, no logic. |
| `openclaw_v1/spot_aggro/spi.py` | 112 | Pure feature computation (squeeze pressure index). No gate logic; feeds L2 state model as one classifier input. |
| `openclaw_v1/spot_aggro/research/mio.py` | 49 | Pure regime-feature computation. Feeds L2 as second classifier input. No gate logic. |
| `openclaw_v1/spot_aggro/research/cycle.py` | 2 | Near-empty placeholder; leave until Phase 4 decides. |
| `openclaw_v1/spot_aggro/research/prompts.py` | — | Research-side prompts only, unrelated to audit swarm. |
| `openclaw_v1/spot_aggro/swarm/models.py` | 147 | `CoinIntel` and `SwarmThreadHealth` dataclasses are reusable by the rebuilt audit swarm. No logic violating Q1–Q5. |
| `openclaw_v1/spot_aggro/forensic_v2/**` | 2378 | **FROZEN by user directive.** Do not touch. |
| `openclaw_v1/shared/persistence/state.py` | 520 | Central SQLite schema including `spot_aggro_coin_memory`, `spot_forensic_runs`, `spot_aggro_regime_log`. KEEP; Phase 5/6 may ADD tables (calibration_table, rejection_log) but must not remove. |
| `openclaw_v1/shared/persistence/settings.py` | — | App settings store. KEEP. |
| `openclaw_v1/shared/adapters/okx_unified.py` | 531 | OKX exchange client. KEEP; only `get_spot_*` methods are called by the spot engine. |
| `openclaw_v1/shared/notifications/router.py` | — | Alert router. KEEP; L0–L4 rejection reasons will be published through it. |
| `openclaw_v1/shared/config/__init__.py` + `spot_aggro_config.yml` | — | Config loader + spot config. KEEP; Phase 3 adds `physics.yml` alongside. |
| `openclaw_v1/shared/llm/clients/*.py` | — | Per-provider HTTP clients (haiku, gpt4o_mini, gemini_flash, deepseek, mistral_small, opus). KEEP; the new audit swarm reuses these clients. |
| `deploy/spot-aggro-v2/build.sh`, `deploy/spot-aggro-v2/index.html` | — | Cloudflare Pages build for the spot dashboard only. KEEP. |

---

## 5. INVENTORY — REWRITE (structure may stay, logic replaced)

Files whose file path stays but whose contents are replaced in later phases to enforce gates.

| File | Lines | Rewrite target | Phase |
|---|---|---|---|
| `openclaw_v1/spot_aggro/engine.py` | 935 | Entry point wrapped by L0 capital gate; `_scan_tiered` calls L1 physics + L2 state + L3 calibration + L6 audit-swarm in that order; every REJECT halts; Tier C branch removed; perp fallback removed; full telemetry dict captured per entry (every L4-required field). No fallback paths. | 2,3,4,5,7 |
| `openclaw_v1/spot_aggro/scoring.py` | 277 | Retain feature computation (SPI, MIO, volume, orderbook) but strip the `compute_composite_score` gate role. `classify_tier` loses the "C" branch. Output becomes inputs to the calibration table, not a direct action. | 5 |
| `openclaw_v1/spot_aggro/coin_memory.py` | 300 | Retain the `(symbol,tier,regime)` bucket schema (it is already the right granularity for L3). Replace the layer-1/2/3 multiplier/cooldown/suppression heuristics with the deterministic VALID/INSUFFICIENT/INVALID table lookup specified by L3. Tier-C entries removed. | 5 |
| `openclaw_v1/spot_aggro/swarm/runner.py` | 432 | Full rewrite as `AuditSwarm` with 4-of-4 hard quorum (LLMs 1–4), 8 s timeout, posterior=0.5 → REJECT, adjudicator (LLM 5) only on unanimous PASS or any UNKNOWN. Health state machine from current `SwarmThreadHealth` is preserved. | 7 |
| `openclaw_v1/spot_aggro/swarm/integration.py` | 132 | Rewrite as a **gate** (`swarm_gate.check(symbol,tier) -> PASS/REJECT+reason`), not a score blender. No `composite_blend_weight`. No `fail-open` branch. | 7 |
| `openclaw_v1/spot_aggro/swarm/prompts.py` | — | Rewrite prompts for the 5 new roles (truth-auditor, trade-physics, state-classifier, calibration, chief-adjudicator). Old prompts referenced composite score — incompatible. | 7 |
| `openclaw_v1/spot_aggro/research/runner.py` | 263 | Retain data assembly, but remove any path that produces an "action" directly. It feeds L2 classifier and telemetry only. | 4 |
| `openclaw_v1/spot_aggro/prompts.py` | 104 | Any prompt that encodes the old composite/tier decision flow is replaced. Prompts for SPI/MIO feature explanations are retained. | 7 |
| `openclaw_v1/shared/llm/consensus.py` | — | Currently used by spot swarm via `_call_member` and by forensic_v2 via `_rough_cost_usd`. Its 600-token cap and provider-fallback logic are incompatible with strict-quorum audit swarm. **Rewrite the parts spot uses** into a spot-local LLM dispatcher (`spot_aggro/swarm/clients.py` in Phase 7) so spot does not share a mutable helper with the other engine. The file itself stays on disk (other engine may use it), but spot stops importing it. | 7 |
| `openclaw_v1/apex_omega/api/routes.py` (spot endpoints only — lines ~520–710, 750–800) | — | **Relocate** spot-aggro HTTP handlers into a spot-owned router (`openclaw_v1/spot_aggro/api/routes.py`, new file). Reason: the separation rule forbids spot endpoints living in an `apex_omega/` path. `server.py` will mount the new router directly. This phase-0 rewrite is structural only; signatures unchanged. | 4 (as part of L2 telemetry wiring) |
| `web/ops/index.html` | — | Add "TRUTH LAYER STATUS" panel for L0–L4 health, live calibration-bucket browser, 4-of-4 quorum meter (red below 4), telemetry completeness %. Remove Tier-C counter/row. | 10 |

---

## 6. HARD-RULE ENFORCEMENT MAP

| Rule | Enforced by file (post-upgrade) |
|---|---|
| R1 no trade below min viable notional | `core/physics_gate.py` (new) |
| R2 no trade with expected_move ≤ 2×cost | `core/physics_gate.py` |
| R3 no trade without ≥2 classifier agreement | `core/state_model.py` + `core/state_gate.py` (new) |
| R4 no trade in DEAD_CHOP / UNSTABLE | `core/state_gate.py` |
| R5 no trade with non-VALID bucket | `core/calibration_gate.py` (new) |
| R6 no trade without 4-of-4 swarm quorum | `spot_aggro/swarm/runner.py` rewrite (Phase 7) |
| R7 no Tier C at startup | `scoring.py` rewrite + `calibration_engine.py` (new) |
| R8 no taker fallback on post-only reject | `engine.py` rewrite (Phase 3) — re-quote only |
| R9 no silent retries | new `rejection_log` table in `shared/persistence/state.py` + engine rewrite |
| R10 no conclusion stronger than weakest telemetry | already enforced in `forensic_v2/**` (frozen) |
| R11 no perp scanner on spot | delete fallback at `engine.py:267–269` |
| R12 no stale-market-data substitution | `engine.py` rewrite — `get_spot_ticker` staleness check, stale → REJECT |

---

## 7. TESTS PLANNED (for later phases — none written in Phase 1)

All tests live under `openclaw_v1/spot_aggro/tests/` (new dir — do not reuse `openclaw_v1/tests/`,
which contains the other engine's and legacy tests).

| Test file | Asserts |
|---|---|
| `test_l0_capital_gate.py` | Engine refuses to start when capital < min_viable; error names the gap. |
| `test_l1_physics_gate.py` | Reject when `expected_move_bp <= 2*round_trip_cost_bp`; reject when `expected_move_bp` is unknown; accept otherwise. |
| `test_l2_state_gate.py` | Reject in DEAD_CHOP, UNSTABLE; size-halved in TRANSITION; require 2-classifier agreement; one-classifier = UNSTABLE. |
| `test_l3_calibration_gate.py` | Reject when bucket status ≠ VALID; reject on missing bucket; accept only VALID. |
| `test_audit_swarm_quorum.py` | Input: 3-of-5 respond → REJECT trade. Input: 4-of-4 PASS → call adjudicator. Input: any UNKNOWN → call adjudicator. |
| `test_posterior_0_5_rejected.py` | A verdict of posterior=0.500 from any path is treated as "no information" and REJECTs the trade. |
| `test_tier_c_disabled_at_startup.py` | Any attempt to enter a Tier-C trade is rejected before reaching any gate. |
| `test_report_unverifiable_propagation.py` | Report with one UNVERIFIABLE section carries the banner; no section is labeled stronger than its weakest input. (Tests against the FROZEN forensic_v2 — assertion only, no code changes to that module.) |
| `test_funding_hunt_disabled_spot.py` | Importing `spot_aggro.engine` never resolves `shared.adapters.universe_discovery`; startup on an empty `cfg.universe` halts with L0 error, does not fall back to perps. |
| `test_integration_5_layer_gate.py` | Full-stack: single synthetic trade must pass all 5 gates; any single gate REJECT blocks order placement; every gate's rejection reason is written to `rejection_log`. |

---

## 8. OPEN QUESTIONS (for operator, before Phase 2)

1. **Spot API router relocation**: confirm moving `/apex/spot_aggro/*` handlers out of
   `apex_omega/api/routes.py` into a new `openclaw_v1/spot_aggro/api/routes.py` is approved. This
   is required by the separation rule but is a structural change beyond the 5-gate spec. If denied,
   the handlers stay where they are and the separation rule is treated as "logical" only for the
   apex routes file.
2. **Shared LLM dispatcher**: `shared/llm/consensus.py` is currently imported by both engines. For
   strict separation, spot gets its own dispatcher in Phase 7. Confirm a copy-and-fork is
   acceptable (no shared mutation surface) vs. keeping a read-only shared helper.
3. **Forensic v1 removal**: the 3 route handlers at `apex_omega/api/routes.py:771/781/797` call the
   legacy `spot_aggro.forensic.runner`. DELETE-listed here. Confirm no dashboard still calls
   `/apex/spot_aggro/forensic/*` (non-v2); if it does, the dashboard is updated in Phase 10 first
   and the routes are removed after.
4. **Capital parameter for L0**: spec lists "$2,000 USD" as the min viable capital for spot
   directional at current parameters. Phase 2 will accept this as the YAML default; confirm or
   provide a different number.
5. **Tier-C shadow-log location**: Phase 5 needs a shadow-log surface that records what Tier-C
   *would have* entered so the 50-trade positive-expectancy threshold can be measured. Planned
   location is a new `spot_aggro_shadow_log` table in `shared/persistence/state.py`. Confirm.

---

## 9. END OF PHASE 1

- No files created, modified, or deleted.
- Only this audit document was written.
- Awaiting operator acknowledgment to proceed to Phase 2 (L0 Capital Viability Gate).
