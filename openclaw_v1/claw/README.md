# Claw — CLAW-NIC-v1

Claw is the audit, monitoring, execution-tracking, and operator-visibility
layer for claw247-trading. Claw is **not** the trading bot. The bot brain
(`strategies/`, `binary15m/`, `binary15/`, `apex_v2/`) remains the sole
authority on scoring, confidence, and trade planning.

## Non-Interference Contract (CLAW-NIC-v1)

Claw **may**:

- ingest bot outputs unchanged
- store them immutably (SHA-256 hashed, append-only table, DB triggers)
- track execution lifecycle (queued → submitted → partial → filled / rejected / canceled → reconciled)
- record fills, rejects, slippage, latency, retries
- monitor API / DB / WS / exchange health
- reconcile local execution rows against exchange state
- show truth in the dashboard (three clearly separated zones)
- log incidents
- provide commentary tagged `source=claw.commentary / authoritative=false`
- self-heal **infrastructure only** (reopen DB, swap WS→REST, rotate logs)

Claw **must NEVER**:

- modify bot scores, confidence, or rationale
- modify the bot's trade plan (entry/stop/target)
- re-score, re-rank, or override bot decisions
- reinterpret itself as authority
- inject hidden decision logic
- self-heal strategy logic

## File-by-file

| File | Purpose |
|---|---|
| [contract.py](contract.py) | `freeze()`, `bot_payload_hash()`, `assert_unchanged()`, `BOT_FIELDS_FROZEN` |
| [schema.sql](schema.sql) | Plane A (bot truth, append-only) + Plane B (claw execution facts, incidents, probes) |
| [db.py](db.py) | Idempotent schema init, SQLite path resolution |
| [ingest.py](ingest.py) | `record_bot_decision()` — the canonical ingest boundary |
| [execution_tracker.py](execution_tracker.py) | Idempotent submit + lifecycle state machine |
| [reconciler.py](reconciler.py) | Read-only resolver + startup recovery |
| [watchdog.py](watchdog.py) | Infra probes + auto-heal (infra only) |
| [incidents.py](incidents.py) | Unified incident log |
| [commentary.py](commentary.py) | Gemini wrapper that always tags `authoritative=false` |
| [api.py](api.py) | FastAPI router — `/claw/*` endpoints |

## DB layout

**Plane A — bot truth (immutable)**
- `bot_decisions_immutable` — `UPDATE` and `DELETE` raise SQL triggers.

**Plane B — claw execution facts (mutable lifecycle)**
- `claw_executions` — per-intent row, idempotency key, state, fills, slippage
- `claw_execution_events` — append-only state-change log
- `claw_incidents` — infra events
- `claw_health_probes` — watchdog snapshots

## API

All endpoints mount under `/claw/*`. Key ones:

- `GET /claw/contract` — the contract definition
- `GET /claw/bot-decisions` — Plane A rows (read-only)
- `POST /claw/ingest/bot-decision` — append to Plane A (idempotent)
- `GET /claw/executions` / `/claw/executions/{id}` — Plane B rows
- `GET /claw/health` / `POST /claw/watchdog/run` — probes
- `GET /claw/incidents` — infra events
- `POST /claw/commentary/{scenario|blend|anomalies|report}` — Gemini, always tagged non-authoritative

## Dashboard

The Overview page now has three named zones:

1. **Bot decision truth** — latest immutable decisions with SHA-256 badges
2. **Claw execution facts** — lifecycle rows, fills, slippage, retries
3. **Claw system health** — probe pills, recent probes, unresolved incidents

Every AI/Gemini output is rendered with a visible `commentary · non-authoritative` chip.

## Tests

- [tests/test_claw_non_interference.py](../tests/test_claw_non_interference.py) — 19 tests proving the contract
- [tests/test_claw_execution_tracker.py](../tests/test_claw_execution_tracker.py) — idempotency, partials, retries
- [tests/test_claw_reconciler.py](../tests/test_claw_reconciler.py) — drift repair, startup recovery
- [tests/test_claw_watchdog.py](../tests/test_claw_watchdog.py) — probes + auto-heal, never touches strategy
