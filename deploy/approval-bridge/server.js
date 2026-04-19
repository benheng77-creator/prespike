/**
 * Claude Code Approval Bridge.
 *
 * Flow:
 *   Claude Code PermissionRequest hook (stdin JSON)
 *   → hook script POSTs to /hook/permission-request
 *   → bridge stores pending request
 *   → sends Telegram message with inline Approve/Reject buttons
 *   → sends CallMeBot WhatsApp alert
 *   → Telegram callback polling picks up button press
 *   → returns allow/deny back to hook script → Claude Code
 */

const express = require("express");
const crypto = require("crypto");
const https = require("https");
const path = require("path");

require("dotenv").config({ path: path.join(__dirname, ".env") });

const PORT = parseInt(process.env.PORT || "48777");
const HOST = process.env.HOST || "127.0.0.1";
const SECRET = process.env.SHARED_SECRET || "";
const TIMEOUT = parseInt(process.env.APPROVAL_TIMEOUT_MS || "900000"); // 15 min
const ALLOWED_ROOT = (process.env.ALLOWED_ROOT || "").replace(/\\/g, "/").toLowerCase();
const CALLMEBOT_PHONE = process.env.CALLMEBOT_PHONE || "";
const CALLMEBOT_KEY = process.env.CALLMEBOT_API_KEY || "";
const TG_TOKEN = process.env.TELEGRAM_BOT_TOKEN || "";
const TG_CHAT = process.env.TELEGRAM_CHAT_ID || "";

const app = express();
app.use(express.json({ limit: "1mb" }));

const pending = new Map(); // id → { resolve, data, token, ts }

// ─── Health ──────────────────────────────────────────────────────────────────
app.get("/health", (_req, res) => {
  res.json({ ok: true, pending: pending.size, uptime_s: Math.floor(process.uptime()) });
});

// ─── Claude hook entry ───────────────────────────────────────────────────────
app.post("/hook/permission-request", (req, res) => {
  if (SECRET && req.headers["x-shared-secret"] !== SECRET) {
    return res.status(401).json({ error: "unauthorized" });
  }

  const body = req.body || {};
  const tool = body.tool_name || body.event?.tool_name || "unknown";
  const input = JSON.stringify(body.tool_input || body.event?.tool_input || {}).slice(0, 400);
  const project = (body.cwd || body.event?.project_path || "").replace(/\\/g, "/");

  // Workspace restriction — normalize Windows/MSYS paths
  const normPath = project.toLowerCase().replace(/^\/([a-z])\//, "$1:/");
  if (ALLOWED_ROOT && !normPath.startsWith(ALLOWED_ROOT)) {
    console.log(`[DENY] outside allowed root: ${project} (norm: ${normPath})`);
    return res.json({ decision: "deny", reason: "outside allowed workspace" });
  }

  const id = crypto.randomBytes(6).toString("hex");
  const ts = Date.now();

  let resolvePromise;
  const promise = new Promise((resolve) => { resolvePromise = resolve; });

  pending.set(id, {
    resolve: resolvePromise,
    data: { tool, input, project, id, ts },
    ts,
  });

  // Auto-expire
  setTimeout(() => {
    if (pending.has(id)) {
      pending.get(id).resolve({ decision: "deny", reason: "timeout" });
      pending.delete(id);
      console.log(`[TIMEOUT] ${id}`);
    }
  }, TIMEOUT);

  const shortProject = project.split("/").slice(-2).join("/");
  console.log(`[PENDING] ${id} tool=${tool} project=${shortProject} (30s local window)`);

  // 30s delay: give VS Code user time to approve locally first.
  // If still pending after 30s, escalate to Telegram + WhatsApp.
  setTimeout(() => {
    if (pending.has(id)) {
      console.log(`[ESCALATE→TG] ${id} tool=${tool} (no local response in 30s)`);
      sendTelegram(id, tool, shortProject, input);
      sendCallMeBot(id, tool, shortProject);
    }
  }, 30000);

  // Wait for user response (local VS Code, Telegram callback, or HTTP)
  promise.then((result) => {
    res.json(result);
  });
});

// ─── HTTP approve/reject (backup — for local browser access) ─────────────────
app.get("/approve/:id", (req, res) => {
  const entry = pending.get(req.params.id);
  if (!entry) return res.status(404).send(htmlPage("Expired", "Request already resolved or timed out."));
  entry.resolve({ decision: "allow" });
  pending.delete(req.params.id);
  console.log(`[APPROVED] ${req.params.id}`);
  res.send(htmlPage("Approved", `Tool <b>${entry.data.tool}</b> approved.`));
});

app.get("/reject/:id", (req, res) => {
  const entry = pending.get(req.params.id);
  if (!entry) return res.status(404).send(htmlPage("Expired", "Request already resolved or timed out."));
  entry.resolve({ decision: "deny", reason: "rejected by operator" });
  pending.delete(req.params.id);
  console.log(`[REJECTED] ${req.params.id}`);
  res.send(htmlPage("Rejected", `Tool <b>${entry.data.tool}</b> rejected.`));
});

// ─── Pending list ────────────────────────────────────────────────────────────
app.get("/pending", (_req, res) => {
  const list = [];
  for (const [id, e] of pending) {
    list.push({ id, tool: e.data.tool, age_s: Math.floor((Date.now() - e.ts) / 1000) });
  }
  res.json({ pending: list });
});

// ─── Telegram: send message with callback buttons ────────────────────────────
function sendTelegram(id, tool, project, input) {
  if (!TG_TOKEN || !TG_CHAT) return;

  const text = [
    `🔐 *Claude Approval Required*`,
    ``,
    `*Tool:* \`${tool}\``,
    `*Project:* ${esc(project)}`,
    `*ID:* \`${id}\``,
    ``,
    `\`\`\``,
    input.slice(0, 250),
    `\`\`\``,
  ].join("\n");

  const keyboard = JSON.stringify({
    inline_keyboard: [[
      { text: "✅ Approve", callback_data: `approve:${id}` },
      { text: "❌ Reject", callback_data: `reject:${id}` },
    ]],
  });

  tgApi("sendMessage", {
    chat_id: TG_CHAT,
    text,
    parse_mode: "Markdown",
    reply_markup: keyboard,
  });
}

// ─── Telegram: poll for callback button presses ──────────────────────────────
let tgOffset = 0;

function pollTelegram() {
  if (!TG_TOKEN) return;

  const body = JSON.stringify({ offset: tgOffset, timeout: 30, allowed_updates: ["callback_query"] });
  const req = https.request({
    hostname: "api.telegram.org",
    path: `/bot${TG_TOKEN}/getUpdates`,
    method: "POST",
    headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
    timeout: 35000,
  }, (res) => {
    let data = "";
    res.on("data", (c) => { data += c; });
    res.on("end", () => {
      try {
        const json = JSON.parse(data);
        if (json.ok && json.result) {
          for (const update of json.result) {
            tgOffset = update.update_id + 1;
            handleCallback(update.callback_query);
          }
        }
      } catch {}
      // Continue polling
      setTimeout(pollTelegram, 500);
    });
  });
  req.on("error", () => { setTimeout(pollTelegram, 5000); });
  req.on("timeout", () => { req.destroy(); setTimeout(pollTelegram, 500); });
  req.write(body);
  req.end();
}

function handleCallback(cb) {
  if (!cb || !cb.data) return;

  const [action, id] = cb.data.split(":");
  if (!id) return;

  // Answer the callback (removes loading spinner on button)
  tgApi("answerCallbackQuery", { callback_query_id: cb.id, text: action === "approve" ? "Approved ✅" : "Rejected ❌" });

  const entry = pending.get(id);
  if (!entry) {
    // Edit message to show expired
    if (cb.message) {
      tgApi("editMessageText", {
        chat_id: cb.message.chat.id,
        message_id: cb.message.message_id,
        text: `⏰ Request \`${id}\` already resolved or expired.`,
        parse_mode: "Markdown",
      });
    }
    return;
  }

  if (action === "approve") {
    entry.resolve({ decision: "allow" });
    console.log(`[APPROVED via TG] ${id}`);
  } else {
    entry.resolve({ decision: "deny", reason: "rejected via Telegram" });
    console.log(`[REJECTED via TG] ${id}`);
  }
  pending.delete(id);

  // Edit message to show result
  if (cb.message) {
    const status = action === "approve" ? "✅ APPROVED" : "❌ REJECTED";
    tgApi("editMessageText", {
      chat_id: cb.message.chat.id,
      message_id: cb.message.message_id,
      text: `${status} — \`${entry.data.tool}\` (${id})`,
      parse_mode: "Markdown",
    });
  }
}

// ─── Telegram API helper ─────────────────────────────────────────────────────
function tgApi(method, payload) {
  const body = JSON.stringify(payload);
  const req = https.request({
    hostname: "api.telegram.org",
    path: `/bot${TG_TOKEN}/${method}`,
    method: "POST",
    headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
  });
  req.write(body);
  req.end();
  req.on("error", (e) => { console.log(`[TG ERR] ${method}: ${e.message}`); });
}

// ─── CallMeBot WhatsApp ──────────────────────────────────────────────────────
function sendCallMeBot(id, tool, project) {
  if (!CALLMEBOT_PHONE || !CALLMEBOT_KEY) return;
  const msg = encodeURIComponent(
    `🔐 Claude Approval\nTool: ${tool}\nProject: ${project}\nID: ${id}`
  );
  const url = `https://api.callmebot.com/whatsapp.php?phone=${CALLMEBOT_PHONE}&text=${msg}&apikey=${CALLMEBOT_KEY}`;
  https.get(url, () => {}).on("error", () => {});
}

// ─── Helpers ─────────────────────────────────────────────────────────────────
function esc(s) { return String(s).replace(/[_*[\]()~`>#+=|{}.!-]/g, "\\$&"); }
function htmlPage(title, body) {
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:system-ui;background:#0d1117;color:#c9d1d9;display:grid;place-items:center;min-height:100vh}
.box{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px;text-align:center;max-width:400px}
h1{font-size:24px;margin-bottom:12px}</style></head>
<body><div class="box"><h1>${title}</h1><p>${body}</p></div></body></html>`;
}

// ─── Start ───────────────────────────────────────────────────────────────────
app.listen(PORT, HOST, () => {
  console.log(`[bridge] http://${HOST}:${PORT}`);
  console.log(`[bridge] telegram: ${TG_TOKEN ? "enabled (polling)" : "disabled"}`);
  console.log(`[bridge] callmebot: ${CALLMEBOT_PHONE ? "enabled" : "disabled"}`);
  console.log(`[bridge] timeout: ${TIMEOUT / 1000}s`);

  // Start Telegram long polling
  if (TG_TOKEN) {
    console.log("[bridge] starting Telegram callback polling...");
    pollTelegram();
  }
});
