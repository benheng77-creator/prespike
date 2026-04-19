-- OpenClaw audit schema. Additive only; existing tables (decisions, trades) are untouched.

CREATE TABLE IF NOT EXISTS openclaw_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    correlation_id TEXT NOT NULL,
    parent_correlation_id TEXT,
    kind TEXT NOT NULL,               -- intent | authorized | dispatched | filled | failed | finalized | retried | escalated | scheduler_run | validator | monitor | report
    phase TEXT,                       -- free-form sub-phase
    verb TEXT,                        -- BUY | SELL | CLOSE | REDUCE | CANCEL | MODIFY | REBALANCE | null
    symbol TEXT,
    side TEXT,
    size REAL,
    px REAL,
    notional_usd REAL,
    session TEXT,                     -- baseline | daytrade | null
    policy_check TEXT,                -- ok | denied | needs_escalation | null
    before_json TEXT,
    after_json TEXT,
    result_json TEXT,
    evidence_bundle_id TEXT,
    error TEXT,
    severity TEXT                     -- info | warn | error | critical | null
);

CREATE INDEX IF NOT EXISTS idx_openclaw_actions_corr ON openclaw_actions(correlation_id);
CREATE INDEX IF NOT EXISTS idx_openclaw_actions_ts   ON openclaw_actions(ts_ms);
CREATE INDEX IF NOT EXISTS idx_openclaw_actions_kind ON openclaw_actions(kind);

CREATE TABLE IF NOT EXISTS openclaw_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    date TEXT NOT NULL,               -- YYYY-MM-DD
    kind TEXT NOT NULL,               -- eod | activity | daytrade
    summary_md TEXT,
    json_path TEXT,
    validator_status TEXT,            -- ok | needs_review | failed
    delivered_channels TEXT,          -- comma-delimited
    correlation_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_openclaw_reports_date ON openclaw_reports(date);

CREATE TABLE IF NOT EXISTS daytrade_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    intent_id TEXT,
    correlation_id TEXT,
    symbol TEXT,
    verb TEXT,
    side TEXT,
    size REAL,
    px REAL,
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_daytrade_activity_session ON daytrade_activity(session_id);
CREATE INDEX IF NOT EXISTS idx_daytrade_activity_ts      ON daytrade_activity(ts_ms);
