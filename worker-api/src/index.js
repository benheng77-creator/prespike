import { computeScore } from "./score.js";

let startedAt = 0;
const uptimeSeconds = () => {
  const now = Date.now();
  if (!startedAt) startedAt = now;
  return Math.floor((now - startedAt) / 1000);
};

const cors = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET,POST,OPTIONS",
  "access-control-allow-headers": "*",
  "cache-control": "no-store",
};

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...cors },
  });

const routes = {
  "/health": () => ({ ok: true }),

  "/status": (env) => ({
    mode: env.CLAW_MODE ?? "paper",
    version: env.CLAW_VERSION ?? "worker-v1",
    uptime_s: uptimeSeconds(),
    db_path: "worker:memory",
    db_present: false,
    decisions_logged: 0,
    trades_logged: 0,
    server_time: Math.floor(Date.now() / 1000),
    note: "worker shim — no trade DB attached; point dashboard at a backend with real data when available",
  }),

  "/positions": () => [],
  "/trades": () => [],

  "/gate": (env) => {
    const threshold = Number(env.CLAW_GATE_THRESHOLD ?? "0.75");
    return {
      status: "NO_DB",
      window_trades: 0,
      hits: 0,
      accuracy: 0.0,
      threshold,
    };
  },

  "/openclaw/state": () => ({
    ok: true,
    actions_count: 0,
    recent_actions: [],
    note: "worker shim — point dashboard at local FastAPI for live OpenClaw audit data",
  }),

  "/openclaw/actions": () => ({
    rows: [],
    note: "worker shim — audit rows only exist on the running trader host",
  }),

  "/reports/today": () => ({
    date: new Date().toISOString().slice(0, 10),
    status: "no_report_yet",
    note: "worker shim — daily report is emitted by the local EOD scheduler",
  }),

  "/daytrade/status": () => ({
    enabled: false,
    session_id: null,
    note: "worker shim — daytrade module attaches to the local supervisor",
  }),

  "/exchanges": () => ({
    active: null,
    configured: {},
    supported: ["binance", "coinbase", "okx", "cryptocom", "independentreserve"],
    note: "worker shim — keys live on the local trader host only",
  }),

  "/binary15m/status": () => ({
    enabled: false,
    count_recent: 0,
    forced_rate: 0,
    primary_rate: 0,
    watch_rate: 0,
    avg_pwin_pct: 0,
    latest: null,
    note: "worker shim — live decisions emit from the local supervisor",
  }),

  "/binary15m/metrics": () => ({
    n: 0, buy_count: 0, sell_count: 0,
    forced_rate: 0, primary_rate: 0, watch_rate: 0, deterministic_rate: 0,
    avg_pwin_pct: 0, avg_ev_r: 0, avg_size_pct: 0, avg_score_total: 0,
    note: "worker shim — run the local FastAPI for live metrics",
  }),

  "/binary15m/promotion": () => ({
    stage: "paper", recommended_next: "same",
    reason: "worker shim — no decisions observed at edge",
    metrics: {n: 0},
  }),

  "/binary15/status": () => ({
    enabled: false, count_recent: 0, execute_rate: 0,
    buy_yes_count: 0, buy_no_count: 0,
    avg_edge: 0, avg_final_f: 0, latest: null,
    note: "worker shim — binary15 Kelly engine runs on the trader host",
  }),

  "/binary15/metrics": () => ({
    n_trades: 0, gross_pnl: 0, net_pnl: 0, realized_edge: 0, expected_edge: 0,
    max_drawdown: 0, time_under_water: 0, sharpe: 0, sortino: 0,
    probability_of_ruin: 0, by_action: {},
    note: "worker shim — Kelly metrics require ledger access",
  }),

  "/models/selection": () => ({
    available: {
      A: "binary15m (directional)",
      B: "binary15 (Kelly)",
      C: "Hybrid (A & B agreement)",
      D: "Model 99-X Apex — Structural Liquidation Arbitrage",
      E: "APEX V2 — Perp-only 3-module regime adaptive",
    },
    selected_persisted: [],
    selected_env: [],
    note: "worker shim — selection lives on the trader host (runtime/active_models.json)",
    persisted_path: null,
  }),

  "/models/apex_v2/status": () => ({
    enabled: false,
    model_id: "APEX-v2",
    model_name: "apex_v2",
    model_type: "tick",
    count_recent: 0,
    initializations: 0,
    runs_started: 0,
    failures: 0,
    shutdowns: 0,
    state: null,
    latest: null,
    note: "worker shim — APEX V2 runs on the trader host with OKX creds",
  }),

  "/models/99x_apex/status": () => ({
    enabled: false,
    model_id: "99-X-Apex",
    model_name: "99x_apex",
    model_type: "tick",
    count_recent: 0,
    strikes: 0,
    entries_placed: 0,
    targets_placed: 0,
    stops_tripped: 0,
    panic_exits: 0,
    positions_closed: 0,
    last_strike: null,
    latest: null,
    note: "worker shim — 99-X Apex telemetry lives on the local trader host",
  }),

  "/backtest/scenarios": () => ({
    scenarios: [
      {code: "BLACK_SWAN",          name: "The Black Swan",                 description: "March 2020 / FTX-style liquidity vacuum, extreme taker slippage."},
      {code: "EUPHORIC_BULL_FLUSH", name: "The Euphoric Bull Flush",        description: "Slow grind up, violent 15% flushes, retail long liquidations."},
      {code: "HIGH_VOL_RANGE",      name: "The High-Vol Ranging Market",    description: "2021-style violent chop, 4-sigma events, fake breakouts."},
      {code: "BEAR_BLEED",          name: "The Bear Market Bleed",          description: "Late 2022 grind down, weak bounces, persistent negative funding."},
      {code: "SIDEWAYS_CHOP",       name: "The Sideways Chop",              description: "Summer 2023, near-zero vol, rare triggers, idle/patience test."},
      {code: "S1_2021_BULL",        name: "S1: 2021 Bull Euphoria",         description: "BTC 30K → 65K, momentum-friendly, positive funding."},
      {code: "S2_2022_BEAR",        name: "S2: 2022 Bear Crash (Luna/FTX)", description: "BTC 48K → 16K, drawdown stress, negative funding."},
      {code: "S3_2023_RECOVERY",    name: "S3: 2023 Recovery Grind",        description: "BTC 16K → 44K, ranging-heavy, grid-friendly."},
      {code: "S4_2024_HALVING_ETF", name: "S4: 2024 Halving + ETF Bull",    description: "BTC 44K → 100K, institutional trend, ETF-driven."},
      {code: "S5_2025_LATE_CYCLE",  name: "S5: 2025 Late Cycle",            description: "BTC 100K → 126K → 60K, distribution + correction, mixed regime."},
    ],
    modes: ["single", "multi", "weighted", "chained", "randomized"],
    boundaries: {capital_min: 1, capital_max: 10000000, days_min: 1, days_max: 1095},
    note: "edge mirror — POST /backtest/run only available on the local FastAPI host",
  }),

  "/engines/status": () => ({
    binary15m:       {enabled: false, note: "worker shim"},
    binary15:        {enabled: false, note: "worker shim"},
    daytrade:        {enabled: false, note: "worker shim"},
    model_99x_apex:  {enabled: false, model_id: "99-X-Apex", note: "worker shim"},
    model_apex_v2:   {enabled: false, model_id: "APEX-v2",   note: "worker shim"},
  }),
};

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });

    const { pathname } = new URL(request.url);

    if (pathname === "/score") {
      if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405);
      let payload;
      try {
        payload = await request.json();
      } catch {
        return json({ error: "invalid_json" }, 400);
      }
      try {
        return json(computeScore(payload));
      } catch (e) {
        return json({ error: "invalid_input", message: String(e?.message ?? e) }, 422);
      }
    }

    const handler = routes[pathname];
    if (!handler) return json({ error: "not_found", path: pathname }, 404);
    if (request.method !== "GET") return json({ error: "method_not_allowed" }, 405);

    try {
      return json(handler(env));
    } catch (e) {
      return json({ error: "internal", message: String(e?.message ?? e) }, 500);
    }
  },
};
