// OpenClaw Control Panel — portable Node HTTP server.
//
// Design:
//   - Zero dependencies. Pure node:* built-ins so the bundle runs on any
//     machine that has Node 22+ installed without `pnpm install`.
//   - Capability-URL auth: every request must come through /p/<TOKEN>/...
//     The token is generated on first launch and stored in state/token.txt.
//     The launcher prints the full panel URL once; the user bookmarks it.
//   - Server-Sent Events stream pushes status updates to the UI so it stays
//     live without manual refresh.
//   - Action runner: a fixed allowlist of named actions (lint/build/test/...),
//     each mapped to a shell command. The UI never sends a raw command — it
//     sends an action id from a dropdown. This is the "no typing" contract.

import { spawn, spawnSync } from "node:child_process";
import crypto from "node:crypto";
import { existsSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const bundleRoot = path.resolve(here, "..");
const stateDir = path.join(bundleRoot, "state");
const publicDir = path.join(here, "public");

if (!existsSync(stateDir)) {
  mkdirSync(stateDir, { recursive: true });
}

const tokenPath = path.join(stateDir, "token.txt");
const tunnelPath = path.join(stateDir, "tunnel.url");
const logPath = path.join(stateDir, "panel.log");
const configPath = path.join(bundleRoot, "config.json");

const config = loadConfig();
const token = loadOrCreateToken();
const port = Number(process.env.OPENCLAW_PANEL_PORT ?? config.port ?? 8787);
const host = process.env.OPENCLAW_PANEL_HOST ?? config.host ?? "127.0.0.1";

const tradingRoot = resolveMaybeRelative(
  process.env.OPENCLAW_TRADING_ROOT ?? config.tradingRoot ?? "../../openclaw_v1",
  bundleRoot,
);
const tradingDb = path.join(tradingRoot, "logs", "openclaw.db");
const tradingHaltFile = path.join(tradingRoot, "cache", ".halt");
const tradingLogFile = path.join(tradingRoot, "logs", "apt_live.log");
const dbQueryScript = path.join(here, "db_query.py");

// Secrets registry — these env files are managed via /api/secrets.
// Adding a new key here makes it appear in the panel UI automatically.
const SECRET_FILES = {
  trading: {
    label: "Trading bot (openclaw_v1/config/.env)",
    path: path.join(tradingRoot, "config", ".env"),
  },
  root: {
    label: "OpenClaw services (./.env)",
    path: path.resolve(projectRootFromConfig(), ".env"),
  },
};

const SECRETS_REGISTRY = [
  // ============================================================
  //   Trading bot — openclaw_v1/config/.env
  // ============================================================
  {
    file: "trading",
    category: "AI / LLM",
    keys: [
      {
        name: "ANTHROPIC_API_KEY",
        label: "Claude (Anthropic) API key",
        hint: "Used by the LLM sentiment feature. Starts with sk-ant-.",
      },
      {
        name: "OPENAI_API_KEY",
        label: "OpenAI API key",
        hint: "Alternative LLM sentiment provider. Starts with sk-.",
      },
    ],
  },
  {
    file: "trading",
    category: "Exchange (live trading)",
    keys: [
      {
        name: "EXCHANGE_ID",
        label: "Exchange id",
        hint: "ccxt exchange id, e.g. binance, bybit, kucoin.",
      },
      {
        name: "API_KEY",
        label: "Primary exchange API key",
        hint: "Public key for the active exchange.",
      },
      {
        name: "API_SECRET",
        label: "Primary exchange API secret",
        hint: "Private secret for the active exchange. Treat like a password.",
      },
      { name: "BYBIT_API_KEY", label: "Bybit API key" },
      { name: "BYBIT_API_SECRET", label: "Bybit API secret" },
      { name: "KUCOIN_API_KEY", label: "KuCoin API key" },
      { name: "KUCOIN_API_SECRET", label: "KuCoin API secret" },
      { name: "KUCOIN_PASSPHRASE", label: "KuCoin passphrase" },
      { name: "OKX_API_KEY", label: "OKX API key" },
      { name: "OKX_API_SECRET", label: "OKX API secret" },
      { name: "OKX_PASSPHRASE", label: "OKX passphrase" },
      { name: "HYPERLIQUID_PRIVATE_KEY", label: "Hyperliquid private key" },
      {
        name: "COINBASE_API_KEY",
        label: "Coinbase API key",
        hint: "Coinbase Advanced Trade API. Older accounts use key+secret; newer accounts use key+PEM private key (set COINBASE_PRIVATE_KEY below).",
      },
      {
        name: "COINBASE_API_SECRET",
        label: "Coinbase API secret",
        hint: "Used by classic Coinbase Pro / Advanced Trade key+secret auth.",
      },
      {
        name: "COINBASE_PRIVATE_KEY",
        label: "Coinbase private key (PEM)",
        hint: "ECDSA private key (PEM) for the new Coinbase Advanced Trade key-based auth. Paste the full PEM including BEGIN/END lines.",
      },
      {
        name: "COINBASE_API_PASSPHRASE",
        label: "Coinbase passphrase",
        hint: "Only required for legacy Coinbase Pro keys. New Advanced Trade keys do not use a passphrase.",
      },
      {
        name: "CRYPTOCOM_API_KEY",
        label: "Crypto.com API key",
        hint: "Crypto.com Exchange API (CCXT id: cryptocom). For app-based withdrawal-only keys, use the Exchange API instead.",
      },
      {
        name: "CRYPTOCOM_API_SECRET",
        label: "Crypto.com API secret",
      },
      {
        name: "INDEPENDENT_RESERVE_API_KEY",
        label: "Independent Reserve API key",
        hint: "Independent Reserve (CCXT id: independentreserve). Get from Settings → API Keys.",
      },
      {
        name: "INDEPENDENT_RESERVE_API_SECRET",
        label: "Independent Reserve API secret",
      },
    ],
  },
  {
    file: "trading",
    category: "Moomoo / Futu (OpenD + OpenAPI — not a CCXT exchange)",
    keys: [
      {
        name: "MOOMOO_HOST",
        label: "OpenD host",
        hint: "Where the local OpenD daemon listens. Default 127.0.0.1 — change only if OpenD runs on another machine.",
      },
      {
        name: "MOOMOO_PORT",
        label: "OpenD port",
        hint: "Default 11111. Must match the port configured in OpenD.",
      },
      {
        name: "MOOMOO_TRADE_PASSWORD",
        label: "Trading unlock password",
        hint: "Plaintext trade password. Required before the SDK can place or cancel orders. Prefer MOOMOO_TRADE_PWD_MD5 if you want to avoid storing plaintext.",
      },
      {
        name: "MOOMOO_TRADE_PWD_MD5",
        label: "Trading unlock password (MD5)",
        hint: "Hex MD5 of your trade password. Use this instead of MOOMOO_TRADE_PASSWORD when possible.",
      },
      {
        name: "MOOMOO_ACCOUNT_ID",
        label: "Trading account id",
        hint: "Numeric account id to route orders through. Leave blank to let the SDK pick the first available account.",
      },
      {
        name: "MOOMOO_MARKET",
        label: "Market",
        hint: "HK, US, CN, or SG — the market the trading account operates in.",
      },
      {
        name: "MOOMOO_TRADE_ENV",
        label: "Trade environment",
        hint: "REAL or SIMULATE. SIMULATE routes to Moomoo's paper account.",
      },
      {
        name: "MOOMOO_RSA_PRIVATE_KEY",
        label: "RSA private key (path or PEM)",
        hint: "Optional. Enables RSA-encrypted protocol to OpenD. Accepts either an absolute file path or the full PEM contents.",
      },
      {
        name: "MOOMOO_OPENAPI_KEY",
        label: "OpenAPI key (cloud)",
        hint: "Only needed if you use Moomoo's cloud OpenAPI instead of a local OpenD instance.",
      },
      {
        name: "MOOMOO_OPENAPI_SECRET",
        label: "OpenAPI secret (cloud)",
        hint: "Paired secret for MOOMOO_OPENAPI_KEY.",
      },
    ],
  },
  {
    file: "trading",
    category: "News & social",
    keys: [
      { name: "NEWS_API_KEY", label: "NewsAPI.org key" },
      { name: "X_BEARER_TOKEN", label: "X (Twitter) bearer token" },
      { name: "REDDIT_CLIENT_ID", label: "Reddit OAuth client id" },
      { name: "REDDIT_CLIENT_SECRET", label: "Reddit OAuth client secret" },
      { name: "REDDIT_USER_AGENT", label: "Reddit user agent string" },
    ],
  },
  {
    file: "trading",
    category: "Market data providers",
    keys: [
      { name: "COINGECKO_API_KEY", label: "CoinGecko API key" },
      { name: "CMC_API_KEY", label: "CoinMarketCap API key" },
      { name: "CRYPTOCOMPARE_API_KEY", label: "CryptoCompare API key" },
      { name: "GLASSNODE_API_KEY", label: "Glassnode API key" },
      { name: "NANSEN_API_KEY", label: "Nansen API key" },
      { name: "ARKHAM_API_KEY", label: "Arkham API key" },
      { name: "DUNE_API_KEY", label: "Dune Analytics API key" },
    ],
  },
  {
    file: "trading",
    category: "Blockchain RPC",
    keys: [
      { name: "ALCHEMY_API_KEY", label: "Alchemy API key" },
      { name: "HELIUS_API_KEY", label: "Helius (Solana) API key" },
      { name: "QUICKNODE_URL", label: "QuickNode RPC URL" },
    ],
  },
  {
    file: "trading",
    category: "Storage",
    keys: [
      {
        name: "DB_URL",
        label: "Database URL",
        hint: "Optional. Falls back to local SQLite at logs/openclaw.db.",
      },
      { name: "REDIS_URL", label: "Redis URL", hint: "Optional. Used for cache + pub/sub if set." },
      { name: "POSTGRES_USER", label: "Postgres user" },
      { name: "POSTGRES_PASSWORD", label: "Postgres password" },
      { name: "POSTGRES_DB", label: "Postgres database name" },
      {
        name: "ENCRYPTION_KEY",
        label: "Encryption key",
        hint: "32-byte random key used by the security vault.",
      },
    ],
  },
  // ============================================================
  //   OpenClaw root services — ./.env
  // ============================================================
  {
    file: "root",
    category: "AI / LLM",
    keys: [
      { name: "ANTHROPIC_API_KEY", label: "Claude (Anthropic) API key" },
      { name: "OPENAI_API_KEY", label: "OpenAI API key" },
      { name: "GEMINI_API_KEY", label: "Google Gemini API key" },
      { name: "MISTRAL_API_KEY", label: "Mistral API key" },
      { name: "OPENROUTER_API_KEY", label: "OpenRouter API key" },
      { name: "PERPLEXITY_API_KEY", label: "Perplexity API key" },
    ],
  },
  {
    file: "root",
    category: "Voice & speech",
    keys: [
      { name: "ELEVENLABS_API_KEY", label: "ElevenLabs API key" },
      { name: "DEEPGRAM_API_KEY", label: "Deepgram API key" },
    ],
  },
  {
    file: "root",
    category: "Search & scraping",
    keys: [
      { name: "BRAVE_API_KEY", label: "Brave Search API key" },
      { name: "FIRECRAWL_API_KEY", label: "Firecrawl API key" },
    ],
  },
  {
    file: "root",
    category: "Messaging integrations",
    keys: [
      { name: "DISCORD_BOT_TOKEN", label: "Discord bot token" },
      { name: "SLACK_BOT_TOKEN", label: "Slack bot token (xoxb-…)" },
      { name: "SLACK_APP_TOKEN", label: "Slack app-level token (xapp-…)" },
      { name: "TELEGRAM_BOT_TOKEN", label: "Telegram bot token" },
      { name: "CALLMEBOT_API_KEY", label: "CallMeBot API key" },
      { name: "CALLMEBOT_PHONE", label: "CallMeBot phone number" },
    ],
  },
  {
    file: "root",
    category: "Internal",
    keys: [
      {
        name: "OPENCLAW_GATEWAY_TOKEN",
        label: "OpenClaw gateway token",
        hint: "Internal — generated automatically if missing.",
      },
    ],
  },
];

function projectRootFromConfig() {
  const v =
    process.env.OPENCLAW_PROJECT_ROOT ?? config.projectRoot ?? path.resolve(bundleRoot, "..", "..");
  return path.isAbsolute(v) ? v : path.resolve(bundleRoot, v);
}

const sseClients = new Set();
const recentRuns = [];
const recentLogs = [];
const MAX_LOGS = 500;
const MAX_RUNS = 50;

const ACTIONS = {
  install: {
    label: "pnpm install",
    description: "Install dependencies (pnpm install).",
    cmd: "pnpm",
    args: ["install"],
  },
  lint: {
    label: "pnpm lint",
    description: "Run oxlint via the project lint runner.",
    cmd: "pnpm",
    args: ["lint"],
  },
  format_check: {
    label: "oxfmt check",
    description: "Check formatting (read-only).",
    cmd: "pnpm",
    args: ["format:check"],
  },
  format_write: {
    label: "oxfmt write",
    description: "Apply formatting fixes.",
    cmd: "pnpm",
    args: ["format"],
  },
  typecheck: {
    label: "tsc (root)",
    description: "TypeScript root project typecheck.",
    cmd: "pnpm",
    args: ["exec", "tsc", "-p", "tsconfig.json", "--noEmit"],
  },
  typecheck_dts: {
    label: "tsc plugin-sdk dts",
    description: "Plugin-SDK declaration emit.",
    cmd: "pnpm",
    args: ["exec", "tsc", "-p", "tsconfig.plugin-sdk.dts.json"],
  },
  build: {
    label: "pnpm build",
    description: "Run the project build.",
    cmd: "pnpm",
    args: ["build"],
  },
  test: {
    label: "pnpm test",
    description: "Run the project test runner.",
    cmd: "pnpm",
    args: ["test"],
  },
  git_status: {
    label: "git status",
    description: "Show working tree status.",
    cmd: "git",
    args: ["status", "--short"],
  },
  git_log: {
    label: "git log (10)",
    description: "Show last 10 commits.",
    cmd: "git",
    args: ["log", "--oneline", "-10"],
  },
  node_version: {
    label: "node --version",
    description: "Show Node.js version.",
    cmd: "node",
    args: ["--version"],
  },
  pnpm_version: {
    label: "pnpm --version",
    description: "Show pnpm version.",
    cmd: "pnpm",
    args: ["--version"],
  },
  disk_usage: {
    label: "Disk usage (project root)",
    description: "Show disk usage of the project root directory.",
    cmd: process.platform === "win32" ? "powershell" : "du",
    args:
      process.platform === "win32"
        ? ["-NoProfile", "-Command", "Get-ChildItem -Force | Measure-Object -Sum Length"]
        : ["-sh", "."],
  },

  trading_resume: {
    label: "Resume trading",
    description: "Delete cache/.halt — engine resumes on next loop.",
    cmd: process.platform === "win32" ? "cmd" : "rm",
    args:
      process.platform === "win32"
        ? ["/c", "if exist cache\\.halt del /q cache\\.halt && echo halt cleared"]
        : ["-f", "cache/.halt"],
    cwd: "trading",
    group: "trading",
  },
  trading_halt: {
    label: "Halt trading",
    description: "Touch cache/.halt — engine refuses new entries on next loop.",
    cmd: process.platform === "win32" ? "cmd" : "sh",
    args:
      process.platform === "win32"
        ? ["/c", "echo halted > cache\\.halt && echo halt set"]
        : ["-c", "mkdir -p cache && touch cache/.halt && echo 'halt set'"],
    cwd: "trading",
    group: "trading",
  },
  // Legacy trading-stack actions removed (spot_v2 uses POST /apex/spot_aggro/start).
  // Underlying scripts (main.py, run_backtest.py, run_calibration.py) remain on
  // disk pending explicit removal — see deferred cleanup plan.
  trading_view_config: {
    label: "View config",
    description: "Print config/openclaw.yaml.",
    cmd: process.platform === "win32" ? "cmd" : "cat",
    args:
      process.platform === "win32"
        ? ["/c", "type config\\openclaw.yaml"]
        : ["config/openclaw.yaml"],
    cwd: "trading",
    group: "trading",
  },
  trading_tail_log: {
    label: "Tail apt_live.log (50)",
    description: "Show the last 50 lines of logs/apt_live.log.",
    cmd: process.platform === "win32" ? "powershell" : "tail",
    args:
      process.platform === "win32"
        ? ["-NoProfile", "-Command", "Get-Content -Tail 50 logs/apt_live.log"]
        : ["-n", "50", "logs/apt_live.log"],
    cwd: "trading",
    group: "trading",
  },
  trading_python_processes: {
    label: "List python processes",
    description: "Show running python processes (host-wide).",
    cmd: process.platform === "win32" ? "tasklist" : "ps",
    args:
      process.platform === "win32" ? ["/FI", "IMAGENAME eq python.exe"] : ["-eo", "pid,rss,cmd"],
    group: "trading",
  },
};

const projectRoot = resolveMaybeRelative(
  process.env.OPENCLAW_PROJECT_ROOT ?? config.projectRoot ?? path.resolve(bundleRoot, "..", ".."),
  bundleRoot,
);

logLine(
  `panel boot. bundleRoot=${bundleRoot} projectRoot=${projectRoot} tradingRoot=${tradingRoot} port=${port}`,
);
logLine(`token (first 6): ${token.slice(0, 6)}…  state/token.txt has full value.`);

const server = http.createServer((req, res) => {
  try {
    handle(req, res);
  } catch (err) {
    logLine(`request error: ${err && err.stack ? err.stack : String(err)}`);
    res.writeHead(500, { "content-type": "text/plain" });
    res.end("internal error");
  }
});

server.listen(port, host, () => {
  logLine(`listening on http://${host}:${port}/p/${token}/`);
});

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);

function shutdown() {
  logLine("shutting down");
  for (const client of sseClients) {
    try {
      client.end();
    } catch {
      /* ignore */
    }
  }
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 1000).unref();
}

function handle(req, res) {
  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  const tokenPrefix = `/p/${token}`;

  if (url.pathname === "/healthz") {
    res.writeHead(200, { "content-type": "text/plain" });
    res.end("ok");
    return;
  }

  if (url.pathname === "/" || url.pathname === "") {
    res.writeHead(401, { "content-type": "text/plain" });
    res.end("openclaw-panel: token required. visit /p/<TOKEN>/");
    return;
  }

  if (!url.pathname.startsWith(`${tokenPrefix}/`) && url.pathname !== tokenPrefix) {
    res.writeHead(401, { "content-type": "text/plain" });
    res.end("openclaw-panel: invalid token");
    return;
  }

  if (url.pathname === tokenPrefix) {
    res.writeHead(302, { location: `${tokenPrefix}/` });
    res.end();
    return;
  }

  const subPath = url.pathname.slice(tokenPrefix.length) || "/";

  if (subPath === "/" || subPath === "") {
    serveStatic(res, "index.html");
    return;
  }

  if (subPath === "/api/state") {
    sendJson(res, currentState());
    return;
  }

  if (subPath === "/api/actions") {
    sendJson(res, actionListPayload());
    return;
  }

  if (subPath === "/api/runs") {
    sendJson(res, { runs: recentRuns });
    return;
  }

  if (subPath === "/api/logs") {
    sendJson(res, { logs: recentLogs });
    return;
  }

  if (subPath === "/api/tunnel") {
    sendJson(res, { url: readTunnelUrl() });
    return;
  }

  if (subPath === "/api/trading") {
    collectTradingState()
      .then((state) => sendJson(res, state))
      .catch((err) => sendJson(res, { ok: false, error: String(err) }, 500));
    return;
  }

  if (subPath === "/api/trading/log") {
    sendJson(res, { lines: tailLogFile(tradingLogFile, 80) });
    return;
  }

  if (subPath === "/api/launch") {
    collectLaunchState()
      .then((state) => sendJson(res, state))
      .catch((err) => sendJson(res, { ok: false, error: String(err) }, 500));
    return;
  }

  if (subPath === "/api/secrets" && req.method === "GET") {
    sendJson(res, listSecrets());
    return;
  }

  if (subPath === "/api/secrets" && req.method === "POST") {
    readJsonBody(req)
      .then((body) => {
        try {
          const result = saveSecret(body);
          sendJson(res, result, result.ok ? 200 : 400);
        } catch (err) {
          sendJson(res, { ok: false, error: String(err) }, 500);
        }
      })
      .catch((err) => sendJson(res, { ok: false, error: String(err) }, 400));
    return;
  }

  if (subPath === "/api/run" && req.method === "POST") {
    readJsonBody(req)
      .then((body) => runAction(body, res))
      .catch((err) => {
        sendJson(res, { ok: false, error: String(err) }, 400);
      });
    return;
  }

  if (subPath === "/api/quit" && req.method === "POST") {
    sendJson(res, { ok: true });
    setTimeout(shutdown, 100);
    return;
  }

  if (subPath === "/api/events") {
    openSse(req, res);
    return;
  }

  if (subPath.startsWith("/static/")) {
    serveStatic(res, subPath.slice("/static/".length));
    return;
  }

  res.writeHead(404, { "content-type": "text/plain" });
  res.end("not found");
}

function loadConfig() {
  if (!existsSync(configPath)) {
    return {};
  }
  try {
    return JSON.parse(readFileSync(configPath, "utf8"));
  } catch (err) {
    logLine(`config parse error: ${err.message}`);
    return {};
  }
}

function loadOrCreateToken() {
  if (existsSync(tokenPath)) {
    const existing = readFileSync(tokenPath, "utf8").trim();
    if (existing.length >= 16) {
      return existing;
    }
  }
  const fresh = crypto.randomBytes(24).toString("base64url");
  writeFileSync(tokenPath, `${fresh}\n`, { mode: 0o600 });
  return fresh;
}

function readTunnelUrl() {
  if (!existsSync(tunnelPath)) {
    return null;
  }
  const value = readFileSync(tunnelPath, "utf8").trim();
  return value || null;
}

function currentState() {
  const tunnelUrl = readTunnelUrl();
  const tokenedTunnel = tunnelUrl ? `${tunnelUrl.replace(/\/+$/, "")}/p/${token}/` : null;
  return {
    panel: {
      version: "1.0.0",
      pid: process.pid,
      uptimeSeconds: Math.round(process.uptime()),
      nodeVersion: process.version,
      platform: `${process.platform} ${process.arch}`,
      hostname: os.hostname(),
      cpuCount: os.cpus().length,
      loadAverage: os.loadavg(),
      memory: {
        totalMb: Math.round(os.totalmem() / (1024 * 1024)),
        freeMb: Math.round(os.freemem() / (1024 * 1024)),
        rssMb: Math.round(process.memoryUsage().rss / (1024 * 1024)),
      },
      bundleRoot,
      projectRoot,
      port,
      host,
    },
    tunnel: {
      base: tunnelUrl,
      panelUrl: tokenedTunnel,
      lastUpdated: existsSync(tunnelPath) ? statSync(tunnelPath).mtime.toISOString() : null,
    },
    actions: actionListPayload().actions,
    recentRuns: recentRuns.slice(0, 10),
  };
}

function actionListPayload() {
  return {
    actions: Object.entries(ACTIONS).map(([id, def]) => ({
      id,
      label: def.label,
      description: def.description,
    })),
  };
}

function runAction(body, res) {
  const id = body && body.id;
  const def = id && ACTIONS[id];
  if (!def) {
    sendJson(res, { ok: false, error: `unknown action: ${id}` }, 400);
    return;
  }
  const runId = `${Date.now().toString(36)}-${crypto.randomBytes(3).toString("hex")}`;
  const run = {
    runId,
    actionId: id,
    label: def.label,
    startedAt: new Date().toISOString(),
    status: "running",
    exitCode: null,
    durationMs: null,
    stdout: "",
    stderr: "",
  };
  recentRuns.unshift(run);
  if (recentRuns.length > MAX_RUNS) {
    recentRuns.pop();
  }

  logLine(`run ${runId} start ${id}`);
  broadcast({ type: "run.start", run });
  sendJson(res, { ok: true, runId });

  const startedAt = Date.now();
  const cwd = def.cwd === "trading" ? tradingRoot : projectRoot;
  const child = spawn(def.cmd, def.args, {
    cwd,
    env: process.env,
    shell: process.platform === "win32",
    detached: Boolean(def.detach),
  });
  if (def.detach) {
    child.unref();
  }

  child.stdout.on("data", (chunk) => {
    const text = chunk.toString();
    run.stdout += text;
    broadcast({ type: "run.output", runId, stream: "stdout", text });
  });
  child.stderr.on("data", (chunk) => {
    const text = chunk.toString();
    run.stderr += text;
    broadcast({ type: "run.output", runId, stream: "stderr", text });
  });
  child.on("error", (err) => {
    run.status = "error";
    run.stderr += `\n[panel] spawn error: ${err.message}\n`;
    run.durationMs = Date.now() - startedAt;
    logLine(`run ${runId} spawn error: ${err.message}`);
    broadcast({ type: "run.end", run });
  });
  child.on("close", (code) => {
    run.status = code === 0 ? "ok" : "fail";
    run.exitCode = code;
    run.durationMs = Date.now() - startedAt;
    logLine(`run ${runId} end code=${code} dur=${run.durationMs}ms`);
    broadcast({ type: "run.end", run });
  });
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk) => {
      data += chunk;
      if (data.length > 64 * 1024) {
        reject(new Error("body too large"));
        req.destroy();
      }
    });
    req.on("end", () => {
      if (!data) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(data));
      } catch (err) {
        reject(err);
      }
    });
    req.on("error", reject);
  });
}

function sendJson(res, payload, status = 200) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "content-type": "application/json",
    "cache-control": "no-store",
    "content-length": Buffer.byteLength(body),
  });
  res.end(body);
}

function serveStatic(res, name) {
  const safe = path
    .normalize(name)
    .replace(/^([a-zA-Z]:)?[\\/]+/, "")
    .replace(/\.\.[\\/]/g, "");
  const file = path.join(publicDir, safe);
  if (!file.startsWith(publicDir)) {
    res.writeHead(403, { "content-type": "text/plain" });
    res.end("forbidden");
    return;
  }
  if (!existsSync(file)) {
    res.writeHead(404, { "content-type": "text/plain" });
    res.end("not found");
    return;
  }
  const ct = contentType(file);
  const data = readFileSync(file);
  res.writeHead(200, {
    "content-type": ct,
    "cache-control": "no-store",
    "content-length": data.length,
  });
  res.end(data);
}

function contentType(file) {
  const ext = path.extname(file).toLowerCase();
  if (ext === ".html") {
    return "text/html; charset=utf-8";
  }
  if (ext === ".js") {
    return "application/javascript; charset=utf-8";
  }
  if (ext === ".css") {
    return "text/css; charset=utf-8";
  }
  if (ext === ".json") {
    return "application/json";
  }
  if (ext === ".svg") {
    return "image/svg+xml";
  }
  return "application/octet-stream";
}

function openSse(req, res) {
  res.writeHead(200, {
    "content-type": "text/event-stream",
    "cache-control": "no-cache",
    connection: "keep-alive",
    "x-accel-buffering": "no",
  });
  res.write(`data: ${JSON.stringify({ type: "hello", state: currentState() })}\n\n`);
  sseClients.add(res);
  const heartbeat = setInterval(() => {
    try {
      res.write(": ping\n\n");
    } catch {
      /* ignore */
    }
  }, 15_000);
  req.on("close", () => {
    clearInterval(heartbeat);
    sseClients.delete(res);
  });
}

function broadcast(event) {
  const payload = `data: ${JSON.stringify(event)}\n\n`;
  for (const client of sseClients) {
    try {
      client.write(payload);
    } catch {
      sseClients.delete(client);
    }
  }
}

function logLine(message) {
  const stamp = new Date().toISOString();
  const line = `${stamp} ${message}`;
  recentLogs.unshift(line);
  if (recentLogs.length > MAX_LOGS) {
    recentLogs.pop();
  }
  try {
    writeFileSync(logPath, `${line}\n`, { flag: "a" });
  } catch {
    /* ignore */
  }
  process.stdout.write(`${line}\n`);
}

function resolveMaybeRelative(value, base) {
  if (!value) {
    return base;
  }
  return path.isAbsolute(value) ? value : path.resolve(base, value);
}

async function collectTradingState() {
  const tradingExists = existsSync(tradingRoot);
  const dbExists = existsSync(tradingDb);
  const halted = existsSync(tradingHaltFile);
  const dbStat = dbExists ? statSync(tradingDb) : null;
  const haltStat = halted ? statSync(tradingHaltFile) : null;

  const state = {
    tradingRoot,
    tradingDb,
    tradingExists,
    dbExists,
    halted,
    haltSetAt: haltStat ? haltStat.mtime.toISOString() : null,
    dbModifiedAt: dbStat ? dbStat.mtime.toISOString() : null,
    dbSizeBytes: dbStat ? dbStat.size : null,
    pythonProcesses: countPythonProcesses(),
    open_trades: [],
    pnl_summary: null,
    latest_decision: null,
    recent_decisions: [],
    decision_count: null,
    trade_count: null,
    balance_latest: null,
    error: null,
  };

  if (!tradingExists) {
    state.error = `tradingRoot not found: ${tradingRoot}`;
    return state;
  }
  if (!dbExists) {
    state.error = `db not found: ${tradingDb}`;
    return state;
  }

  const queries = [
    "open_trades",
    "pnl_summary",
    "latest_decision",
    "recent_decisions",
    "decision_count",
    "trade_count",
    "balance_latest",
    "today_trades",
    "today_pnl",
    "daily_pnl_history",
    "today_decisions_breakdown",
  ];
  state.today_trades = [];
  state.today_pnl = null;
  state.daily_pnl_history = [];
  state.today_decisions_breakdown = [];
  const results = await Promise.all(queries.map((q) => runDbQuery(q)));
  for (let i = 0; i < queries.length; i += 1) {
    const q = queries[i];
    const r = results[i];
    if (r && Array.isArray(r.rows)) {
      if (
        q === "open_trades" ||
        q === "recent_decisions" ||
        q === "today_trades" ||
        q === "daily_pnl_history" ||
        q === "today_decisions_breakdown"
      ) {
        state[q] = r.rows;
      } else if (
        q === "pnl_summary" ||
        q === "latest_decision" ||
        q === "balance_latest" ||
        q === "today_pnl"
      ) {
        state[q] = r.rows[0] ?? null;
      } else if (q === "decision_count" || q === "trade_count") {
        state[q] = r.rows[0] ? r.rows[0].n : null;
      }
    } else if (r && r.error) {
      state.error = state.error ? `${state.error}; ${q}: ${r.error}` : `${q}: ${r.error}`;
    }
  }
  return state;
}

function runDbQuery(name) {
  return new Promise((resolve) => {
    const child = spawn("python", [dbQueryScript, tradingDb, name], {
      cwd: tradingRoot,
      env: process.env,
      shell: process.platform === "win32",
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", (err) => {
      resolve({ error: `spawn: ${err.message}` });
    });
    child.on("close", () => {
      try {
        resolve(JSON.parse(stdout));
      } catch {
        resolve({ error: stderr.trim() || "invalid json" });
      }
    });
  });
}

function tailLogFile(file, n) {
  if (!existsSync(file)) {
    return [`(no log file at ${file})`];
  }
  try {
    const data = readFileSync(file, "utf8");
    const lines = data.split(/\r?\n/);
    return lines.slice(Math.max(0, lines.length - n));
  } catch (err) {
    return [`(log read error: ${err.message})`];
  }
}

// ============================================================
//  Secrets management — read/write .env files in place
// ============================================================

function parseEnvFile(filePath) {
  if (!existsSync(filePath)) {
    return { lines: [], values: new Map() };
  }
  const text = readFileSync(filePath, "utf8");
  const rawLines = text.split(/\r?\n/);
  const values = new Map();
  for (const line of rawLines) {
    const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (!m) {
      continue;
    }
    let value = m[2];
    if (
      value.length >= 2 &&
      ((value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'")))
    ) {
      value = value.slice(1, -1);
    }
    values.set(m[1], value);
  }
  return { lines: rawLines, values };
}

function maskValue(value) {
  if (value == null || value === "") {
    return null;
  }
  const trimmed = String(value);
  if (trimmed.length <= 6) {
    return "•".repeat(trimmed.length);
  }
  if (trimmed.length <= 12) {
    return `${trimmed.slice(0, 2)}${"•".repeat(trimmed.length - 4)}${trimmed.slice(-2)}`;
  }
  return `${trimmed.slice(0, 4)}${"•".repeat(8)}${trimmed.slice(-4)}`;
}

function listSecrets() {
  const filesMeta = {};
  const filesParsed = {};
  for (const [id, def] of Object.entries(SECRET_FILES)) {
    const parsed = parseEnvFile(def.path);
    filesParsed[id] = parsed;
    const stat = existsSync(def.path) ? statSync(def.path) : null;
    filesMeta[id] = {
      label: def.label,
      path: def.path,
      exists: existsSync(def.path),
      sizeBytes: stat ? stat.size : 0,
      modifiedAt: stat ? stat.mtime.toISOString() : null,
      keysOnDisk: parsed.values.size,
    };
  }

  const groups = SECRETS_REGISTRY.map((group) => {
    const parsed = filesParsed[group.file];
    return {
      file: group.file,
      fileLabel: filesMeta[group.file]?.label,
      category: group.category,
      keys: group.keys.map((k) => {
        const value = parsed?.values.get(k.name);
        return {
          name: k.name,
          label: k.label,
          hint: k.hint || "",
          set: value != null && value !== "",
          preview: maskValue(value),
        };
      }),
    };
  });

  return { files: filesMeta, groups };
}

function saveSecret(body) {
  if (!body || typeof body !== "object") {
    return { ok: false, error: "missing body" };
  }
  const { file, name, value } = body;
  if (!SECRET_FILES[file]) {
    return { ok: false, error: `unknown file: ${file}` };
  }
  if (typeof name !== "string" || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) {
    return { ok: false, error: `invalid key name: ${name}` };
  }
  if (typeof value !== "string") {
    return { ok: false, error: "value must be a string (use empty string to delete)" };
  }

  const filePath = SECRET_FILES[file].path;
  if (!existsSync(filePath)) {
    mkdirSync(path.dirname(filePath), { recursive: true });
    writeFileSync(filePath, "", { mode: 0o600 });
  }

  const parsed = parseEnvFile(filePath);
  const lines = parsed.lines.length > 0 ? [...parsed.lines] : [];
  const escaped = formatEnvValue(value);
  const replacement = `${name}=${escaped}`;

  let replacedAt = -1;
  for (let i = 0; i < lines.length; i += 1) {
    const m = lines[i].match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=/);
    if (m && m[1] === name) {
      replacedAt = i;
      break;
    }
  }

  const action = value === "" ? "delete" : replacedAt >= 0 ? "update" : "create";

  if (value === "") {
    if (replacedAt >= 0) {
      lines.splice(replacedAt, 1);
    }
  } else if (replacedAt >= 0) {
    lines[replacedAt] = replacement;
  } else {
    if (lines.length > 0 && lines[lines.length - 1] !== "") {
      lines.push("");
    }
    lines.push(replacement);
  }

  const out = lines.join("\n");
  const tmpPath = `${filePath}.tmp-${process.pid}-${Date.now()}`;
  writeFileSync(tmpPath, out, { mode: 0o600 });
  renameSync(tmpPath, filePath);

  logLine(`secret ${action} ${file}:${name}`);
  return { ok: true, action, name, file, preview: maskValue(value) };
}

function formatEnvValue(value) {
  if (value === "") {
    return "";
  }
  if (/[\s"'#$`\\]/.test(value)) {
    return `"${value.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`;
  }
  return value;
}

// ============================================================
//  Launch control — scanner rankings, accuracy gate, live executor
//  All three are surfaces over data the bot writes; the panel reads
//  the SQLite DB / config yaml / halt file / scanner ranking file
//  directly so it stays honest even if the bot is not running.
// ============================================================

const tradingConfigYaml = path.join(tradingRoot, "config", "openclaw.yaml");
const scannerRankingFile = path.join(tradingRoot, "cache", "scanner_ranking.json");
const godModeResultsFile = path.join(tradingRoot, "logs", "god_mode_results.json");

async function collectLaunchState() {
  const cfg = readTradingConfig();
  const gateCfg = cfg.accuracy_gate || {};
  const liveCfg = cfg.live_trading || {};
  const scannerCfg = cfg.scanner || {};

  const universe =
    Array.isArray(scannerCfg.universe) && scannerCfg.universe.length > 0
      ? scannerCfg.universe
      : [cfg?.exchange?.symbol].filter(Boolean);

  const accuracy = {
    config: {
      enabled: gateCfg.enabled ?? true,
      window_size: gateCfg.window_size ?? 30,
      floor_pct: gateCfg.floor_pct ?? 0.75,
      min_trades_before_floor: gateCfg.min_trades_before_floor ?? 10,
      proving_wins: gateCfg.proving_wins ?? 5,
      cooldown_bars: gateCfg.cooldown_bars ?? 48,
    },
    symbols: [],
  };
  if (universe.length > 0) {
    const results = await Promise.all(universe.map((sym) => gateSymbolState(sym, accuracy.config)));
    accuracy.symbols = results;
  }

  const halted = existsSync(tradingHaltFile);
  const envFlag = liveCfg.env_flag || "OPENCLAW_LIVE_TRADING";
  const envFlagValue = process.env[envFlag] || "";
  const gatesOpen = {
    mode_live: (cfg.mode || "paper") === "live",
    config_live_trading_enabled: liveCfg.enabled === true,
    env_flag_set: envFlagValue.trim() === "1",
    halt_file_absent: !halted,
  };
  const openCount = Object.values(gatesOpen).filter(Boolean).length;
  const executor = {
    config: {
      enabled: liveCfg.enabled ?? false,
      max_notional_quote: liveCfg.max_notional_quote ?? 50.0,
      max_trades_per_day: liveCfg.max_trades_per_day ?? 5,
      reconcile_timeout_s: liveCfg.reconcile_timeout_s ?? 10.0,
      reconcile_tolerance_pct: liveCfg.reconcile_tolerance_pct ?? 0.01,
      env_flag: envFlag,
    },
    gates: [
      {
        id: "mode_live",
        label: "config.mode",
        open: gatesOpen.mode_live,
        detail: `config.mode = ${cfg.mode || "paper"}`,
        where: "openclaw_v1/config/openclaw.yaml",
      },
      {
        id: "config_enabled",
        label: "live_trading.enabled",
        open: gatesOpen.config_live_trading_enabled,
        detail: liveCfg.enabled === true ? "enabled" : "false",
        where: "openclaw_v1/config/openclaw.yaml",
      },
      {
        id: "env_flag",
        label: `env ${envFlag}`,
        open: gatesOpen.env_flag_set,
        detail: gatesOpen.env_flag_set ? "set to 1" : "unset or not 1",
        where: "process environment",
      },
      {
        id: "no_halt_file",
        label: "no halt file",
        open: gatesOpen.halt_file_absent,
        detail: halted ? "cache/.halt present" : "cache/.halt absent",
        where: "filesystem",
      },
    ],
    open_count: openCount,
    total: 4,
    can_go_live: openCount === 4,
    active: openCount === 4,
  };

  const scanner = {
    config: {
      enabled: scannerCfg.enabled ?? false,
      mode: scannerCfg.mode || "recommend",
      universe,
      rank_by: scannerCfg.rank_by || "score_x_ev",
    },
    ranking: readScannerRanking(),
  };

  const prover = readGodModeResults();

  return {
    generated_at: new Date().toISOString(),
    trading_mode: cfg.mode || "paper",
    halted,
    accuracy,
    executor,
    scanner,
    prover,
  };
}

function readGodModeResults() {
  if (!existsSync(godModeResultsFile)) {
    return null;
  }
  try {
    const raw = readFileSync(godModeResultsFile, "utf8");
    const data = JSON.parse(raw);
    return {
      generated_at_ms: data.generated_at_ms,
      accuracy_floor: data.accuracy_floor,
      min_trades_for_verdict: data.min_trades_for_verdict,
      universe: data.universe,
      timeframe: data.timeframe,
      results: data.results,
      earned_live_symbols: data.earned_live_symbols || [],
      levels: (data.levels || []).map((l) => l.name),
    };
  } catch (err) {
    return { error: `failed to parse: ${err.message}` };
  }
}

function readTradingConfig() {
  if (!existsSync(tradingConfigYaml)) {
    return {};
  }
  try {
    const raw = readFileSync(tradingConfigYaml, "utf8");
    return parseSimpleYaml(raw);
  } catch (err) {
    logLine(`readTradingConfig error: ${err.message}`);
    return {};
  }
}

function readScannerRanking() {
  if (!existsSync(scannerRankingFile)) {
    return null;
  }
  try {
    const raw = readFileSync(scannerRankingFile, "utf8");
    return JSON.parse(raw);
  } catch (err) {
    return { error: `failed to parse: ${err.message}` };
  }
}

async function gateSymbolState(symbol, cfg) {
  try {
    const row = await runGateSnapshot(symbol, cfg.window_size);
    if (row.error) {
      return { symbol, error: row.error };
    }
    const rollingWr = row.window_trades > 0 ? row.window_wins / row.window_trades : 0;
    const enoughSamples = row.window_trades >= cfg.min_trades_before_floor;
    const floorSatisfied = !enoughSamples || rollingWr >= cfg.floor_pct;
    const provingSatisfied = row.total_wins >= cfg.proving_wins;
    return {
      symbol,
      window_trades: row.window_trades,
      window_wins: row.window_wins,
      window_losses: row.window_losses,
      total_closed: row.total_trades_closed,
      total_wins: row.total_wins,
      rolling_wr: rollingWr,
      floor_satisfied: floorSatisfied,
      proving_satisfied: provingSatisfied,
      paper_allowed: floorSatisfied,
      live_allowed: floorSatisfied && provingSatisfied,
    };
  } catch (err) {
    return { symbol, error: String(err) };
  }
}

function runGateSnapshot(symbol, window) {
  return new Promise((resolve, reject) => {
    const args = [dbQueryScript, tradingDb, "gate_snapshot", symbol, String(window)];
    const child = spawn("python", args, {
      cwd: tradingRoot,
      shell: process.platform === "win32",
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.stderr.on("data", (c) => (stderr += c));
    child.on("error", (err) => reject(err));
    child.on("close", () => {
      try {
        resolve(JSON.parse(stdout));
      } catch {
        resolve({ error: stderr.trim() || "invalid json" });
      }
    });
  });
}

function parseSimpleYaml(text) {
  // Minimal YAML parser: handles key: value, nested 2-space indented objects,
  // and `- item` lists. Enough to read openclaw.yaml. Does not support
  // anchors, flow style, or multi-line strings.
  const lines = text.split(/\r?\n/);
  const root = {};
  const stack = [{ indent: -1, node: root, key: null, parent: null }];
  for (const rawLine of lines) {
    const line = rawLine.replace(/#.*$/, "").replace(/\s+$/, "");
    if (!line.trim()) {
      continue;
    }
    const indent = rawLine.match(/^\s*/)[0].length;
    while (stack.length > 1 && indent <= stack[stack.length - 1].indent) {
      stack.pop();
    }
    const top = stack[stack.length - 1];
    const listMatch = line.trim().match(/^-\s*(.*)$/);
    if (listMatch) {
      let container = top.node;
      // If we pushed `key:` as an empty object and this is the first child,
      // retroactively convert it to an array.
      if (
        !Array.isArray(container) &&
        top.parent &&
        top.key != null &&
        Object.keys(container).length === 0
      ) {
        const arr = [];
        top.parent[top.key] = arr;
        top.node = arr;
        container = arr;
      }
      if (!Array.isArray(container)) {
        continue;
      }
      const val = coerceScalar(listMatch[1]);
      container.push(val);
      continue;
    }
    const kv = line.trim().match(/^([A-Za-z_][A-Za-z0-9_.-]*)\s*:\s*(.*)$/);
    if (!kv) {
      continue;
    }
    const key = kv[1];
    const val = kv[2];
    const parent = top.node;
    if (Array.isArray(parent)) {
      continue;
    }
    if (val === "") {
      const fresh = {};
      parent[key] = fresh;
      stack.push({ indent, node: fresh, key, parent });
    } else if (val === "[]") {
      parent[key] = [];
    } else {
      parent[key] = coerceScalar(val);
    }
  }
  return root;
}

function coerceScalar(raw) {
  const value = raw.trim();
  if (value === "") {
    return "";
  }
  if (value === "true" || value === "True") {
    return true;
  }
  if (value === "false" || value === "False") {
    return false;
  }
  if (value === "null" || value === "~") {
    return null;
  }
  if (
    value.length >= 2 &&
    ((value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'")))
  ) {
    return value.slice(1, -1);
  }
  if (/^-?\d+$/.test(value)) {
    return Number(value);
  }
  if (/^-?\d+\.\d+$/.test(value)) {
    return Number(value);
  }
  return value;
}

function countPythonProcesses() {
  try {
    const proc =
      process.platform === "win32"
        ? spawnSync("tasklist", ["/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"], {
            encoding: "utf8",
          })
        : spawnSync("pgrep", ["-c", "-x", "python"], { encoding: "utf8" });
    if (proc.status === 0 || proc.status === 1) {
      const out = (proc.stdout || "").trim();
      if (process.platform === "win32") {
        if (!out || out.includes("INFO:")) {
          return 0;
        }
        return out.split(/\r?\n/).length;
      }
      return Number(out) || 0;
    }
  } catch {
    /* ignore */
  }
  return null;
}
