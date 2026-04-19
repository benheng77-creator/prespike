-- CLAW-NIC-v1 — Plane A: Bot-truth tables (immutable, append-only).
-- This file is idempotent. Safe to execute on every boot.

CREATE TABLE IF NOT EXISTS bot_decisions_immutable (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms            INTEGER NOT NULL,
    strategy_id      TEXT NOT NULL,
    cycle_id         TEXT,
    symbol           TEXT,
    payload_json     TEXT NOT NULL,       -- full bot payload, verbatim
    payload_sha256   TEXT NOT NULL,       -- hash over BOT_FIELDS_FROZEN subset
    signature        TEXT,                -- optional HMAC / RSA signature
    ingest_source    TEXT NOT NULL,       -- where the payload came from
    correlation_id   TEXT,                -- audit ledger cross-ref
    UNIQUE(strategy_id, payload_sha256)   -- natural idempotency key
);

CREATE INDEX IF NOT EXISTS idx_bot_decisions_ts
    ON bot_decisions_immutable(ts_ms);

CREATE INDEX IF NOT EXISTS idx_bot_decisions_strategy
    ON bot_decisions_immutable(strategy_id, ts_ms);

CREATE INDEX IF NOT EXISTS idx_bot_decisions_correlation
    ON bot_decisions_immutable(correlation_id);

-- CLAW-NIC-v1 enforcement: any UPDATE or DELETE on this table is a contract
-- violation. The triggers make mutation impossible at the DB layer, even if
-- a caller bypasses the Python guard.

CREATE TRIGGER IF NOT EXISTS trg_no_update_bot_decisions
BEFORE UPDATE ON bot_decisions_immutable
BEGIN
    SELECT RAISE(ABORT,
        'CLAW-NIC-v1: bot_decisions_immutable is append-only, UPDATE denied');
END;

CREATE TRIGGER IF NOT EXISTS trg_no_delete_bot_decisions
BEFORE DELETE ON bot_decisions_immutable
BEGIN
    SELECT RAISE(ABORT,
        'CLAW-NIC-v1: bot_decisions_immutable is append-only, DELETE denied');
END;


-- ---------------------------------------------------------------------------
-- CLAW-NIC-v1 — Plane B: Claw execution facts (mutable lifecycle).
-- Everything Claw DOES in response to a bot decision lives here. Never
-- references the bot brain; only the bot_decision_id FK into Plane A.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS claw_executions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts_ms         INTEGER NOT NULL,
    updated_ts_ms         INTEGER NOT NULL,
    bot_decision_id       INTEGER,                 -- FK -> bot_decisions_immutable.id
    correlation_id        TEXT,
    idempotency_key       TEXT NOT NULL,           -- sha256 of routing inputs
    strategy_id           TEXT NOT NULL,
    symbol                TEXT NOT NULL,
    side                  TEXT NOT NULL,           -- BUY | SELL | CLOSE | REDUCE
    requested_qty         REAL NOT NULL,
    filled_qty            REAL NOT NULL DEFAULT 0.0,
    avg_fill_px           REAL,
    requested_px          REAL,
    slippage_bps          REAL,
    state                 TEXT NOT NULL,           -- see state machine below
    exchange              TEXT,
    exchange_order_id     TEXT,
    submitted_ts_ms       INTEGER,
    ws_ack_ts_ms          INTEGER,
    rest_ack_ts_ms        INTEGER,
    final_ts_ms           INTEGER,
    retries               INTEGER NOT NULL DEFAULT 0,
    last_error            TEXT,
    rejected_reason       TEXT,
    metadata_json         TEXT,
    UNIQUE(strategy_id, idempotency_key)
);

-- State machine:
--   queued -> submitted -> (ack_ws|ack_rest) -> partial* -> filled
--                                                       -> rejected
--                                                       -> canceled
--   reconciled is set by the reconciler after cross-check vs exchange.

CREATE INDEX IF NOT EXISTS idx_claw_exec_state
    ON claw_executions(state, created_ts_ms);
CREATE INDEX IF NOT EXISTS idx_claw_exec_symbol
    ON claw_executions(symbol, created_ts_ms);
CREATE INDEX IF NOT EXISTS idx_claw_exec_bot_decision
    ON claw_executions(bot_decision_id);


-- Append-only lifecycle event log for each execution row. We never update
-- claw_execution_events; it captures the full transition history.
CREATE TABLE IF NOT EXISTS claw_execution_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id      INTEGER NOT NULL,
    ts_ms             INTEGER NOT NULL,
    kind              TEXT NOT NULL,      -- queued|submitted|ack_ws|ack_rest|partial|filled|rejected|canceled|reconciled|retry
    payload_json      TEXT,
    FOREIGN KEY(execution_id) REFERENCES claw_executions(id)
);

CREATE INDEX IF NOT EXISTS idx_claw_exec_events_exec
    ON claw_execution_events(execution_id, ts_ms);


-- Infra incidents (watchdog). Never references bot logic.
CREATE TABLE IF NOT EXISTS claw_incidents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    kind            TEXT NOT NULL,        -- db|ws|rest|exchange|clock|disk|other
    severity        TEXT NOT NULL,        -- info|warn|error|critical
    component       TEXT,
    message         TEXT NOT NULL,
    auto_action     TEXT,                 -- infra-only remediation taken
    resolved_ts_ms  INTEGER,
    metadata_json   TEXT
);

CREATE INDEX IF NOT EXISTS idx_claw_incidents_ts
    ON claw_incidents(ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_claw_incidents_unresolved
    ON claw_incidents(resolved_ts_ms, ts_ms);


-- Health probes (watchdog). Per-probe snapshot, rotated by row count.
CREATE TABLE IF NOT EXISTS claw_health_probes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    probe           TEXT NOT NULL,        -- db|fastapi|ws|exchange|clock|disk
    ok              INTEGER NOT NULL,     -- 1=ok, 0=fail
    latency_ms      REAL,
    detail          TEXT
);

CREATE INDEX IF NOT EXISTS idx_claw_health_ts
    ON claw_health_probes(probe, ts_ms DESC);
