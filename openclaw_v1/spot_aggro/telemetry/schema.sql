-- ============================================================================
-- SPOT AGGRO telemetry tables (QuestDB).
-- Every timestamp is microsecond epoch (QuestDB native). Partition by day WAL.
-- Table names are prefixed spot_ — the apex_omega namespace is never touched.
-- ============================================================================

CREATE TABLE IF NOT EXISTS spot_trades (
  ts        TIMESTAMP,
  action    SYMBOL CAPACITY 16 INDEX,
  symbol    SYMBOL CAPACITY 64 INDEX,
  tier      SYMBOL CAPACITY 8,
  module    SYMBOL CAPACITY 32,
  notional_usd DOUBLE,
  avg_px    DOUBLE,
  fee_usd   DOUBLE,
  pnl_usd   DOUBLE,
  reason    SYMBOL CAPACITY 64,
  composite DOUBLE,
  spi       DOUBLE,
  correlation_id SYMBOL CAPACITY 32768
) TIMESTAMP(ts) PARTITION BY DAY WAL;

CREATE TABLE IF NOT EXISTS spot_funnel (
  ts              TIMESTAMP,
  window_min      INT,
  scored          LONG,
  tier_passed     LONG,
  consensus_fired LONG,
  consensus_passed LONG,
  orders_entered  LONG,
  orders_rejected LONG,
  orders_exited   LONG
) TIMESTAMP(ts) PARTITION BY DAY WAL;

CREATE TABLE IF NOT EXISTS spot_audit_swarm (
  ts          TIMESTAMP,
  symbol      SYMBOL CAPACITY 64 INDEX,
  verdict     SYMBOL CAPACITY 8,
  reason_code SYMBOL CAPACITY 16,
  quorum_size INT,
  required_quorum INT,
  posterior_final DOUBLE,
  total_cost_usd DOUBLE,
  total_latency_ms INT,
  adjudicator_invoked BOOLEAN
) TIMESTAMP(ts) PARTITION BY DAY WAL;

CREATE TABLE IF NOT EXISTS spot_swarm_cycles (
  ts           TIMESTAMP,
  layer        SYMBOL CAPACITY 8,
  duration_ms  INT,
  agents_ok    INT,
  agents_total INT,
  coins_cycled INT,
  cost_usd     DOUBLE,
  consecutive_errors INT,
  last_error   SYMBOL CAPACITY 64
) TIMESTAMP(ts) PARTITION BY DAY WAL;

CREATE TABLE IF NOT EXISTS spot_consensus (
  ts        TIMESTAMP,
  symbol    SYMBOL CAPACITY 64 INDEX,
  consensus DOUBLE,
  conflict  DOUBLE,
  vetoed    BOOLEAN,
  members_called INT,
  members_ok INT
) TIMESTAMP(ts) PARTITION BY DAY WAL;

CREATE TABLE IF NOT EXISTS spot_regime (
  ts                 TIMESTAMP,
  regime             SYMBOL CAPACITY 32 INDEX,
  regime_confidence  DOUBLE,
  squeeze_timing     SYMBOL CAPACITY 16,
  edge_status        SYMBOL CAPACITY 16,
  universe_quality   SYMBOL CAPACITY 16
) TIMESTAMP(ts) PARTITION BY DAY WAL;

CREATE TABLE IF NOT EXISTS spot_execution_cost (
  ts             TIMESTAMP,
  symbol         SYMBOL CAPACITY 64 INDEX,
  notional_usd   DOUBLE,
  ref_price      DOUBLE,
  fill_price     DOUBLE,
  slippage_bp    DOUBLE,
  half_spread_bp DOUBLE,
  fee_bp         DOUBLE,
  maker_or_taker SYMBOL CAPACITY 8,
  round_trip_cost_bp DOUBLE,
  expected_move_bp   DOUBLE
) TIMESTAMP(ts) PARTITION BY DAY WAL;

CREATE TABLE IF NOT EXISTS spot_coin_memory_log (
  ts         TIMESTAMP,
  symbol     SYMBOL CAPACITY 64 INDEX,
  tier       SYMBOL CAPACITY 8,
  regime     SYMBOL CAPACITY 32,
  n_trades   INT,
  wins       INT,
  win_rate   DOUBLE,
  sum_pnl    DOUBLE,
  composite_mult DOUBLE,
  cooldown_until_ts TIMESTAMP,
  suppressed BOOLEAN
) TIMESTAMP(ts) PARTITION BY DAY WAL;

CREATE TABLE IF NOT EXISTS spot_forensic_runs (
  ts          TIMESTAMP,
  report_id   SYMBOL CAPACITY 32768,
  window_h    DOUBLE,
  verdict     SYMBOL CAPACITY 32,
  n_trades    INT,
  win_rate    DOUBLE,
  exp_per_trade DOUBLE,
  quorum_ok   INT,
  quorum_total INT,
  cost_usd    DOUBLE,
  governor_verdict SYMBOL CAPACITY 32,
  trust_score DOUBLE
) TIMESTAMP(ts) PARTITION BY DAY WAL;

CREATE TABLE IF NOT EXISTS spot_kill_events (
  ts     TIMESTAMP,
  kind   SYMBOL CAPACITY 16,
  reason SYMBOL CAPACITY 64,
  dd_pct DOUBLE,
  equity_usd DOUBLE
) TIMESTAMP(ts) PARTITION BY DAY WAL;

CREATE TABLE IF NOT EXISTS spot_wri_runs (
  ts        TIMESTAMP,
  cadence   SYMBOL CAPACITY 8,
  n_trades  INT,
  win_rate  DOUBLE,
  total_pnl DOUBLE,
  top_cause SYMBOL CAPACITY 32,
  confidence SYMBOL CAPACITY 16
) TIMESTAMP(ts) PARTITION BY DAY WAL;

CREATE TABLE IF NOT EXISTS spot_governor_runs (
  ts          TIMESTAMP,
  kind        SYMBOL CAPACITY 16,
  report_id   SYMBOL CAPACITY 32768,
  verdict     SYMBOL CAPACITY 32,
  trust_score DOUBLE,
  n_unverifiable INT,
  n_contradictions INT,
  n_unsupported INT
) TIMESTAMP(ts) PARTITION BY DAY WAL;

CREATE TABLE IF NOT EXISTS spot_dashboard_truth_issues (
  ts       TIMESTAMP,
  kind     SYMBOL CAPACITY 16,
  card     SYMBOL CAPACITY 64 INDEX,
  severity SYMBOL CAPACITY 8,
  summary_value DOUBLE,
  body_value    DOUBLE,
  age_s    INT
) TIMESTAMP(ts) PARTITION BY DAY WAL;
