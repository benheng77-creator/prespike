/**
 * APEX-Ω Cloud Worker — state ingestion + public API.
 *
 * Desktop pushes snapshots via POST /ingest (auth'd).
 * Dashboard reads via GET /api/* (public, read-only).
 *
 * KV namespace: APEX_STATE
 *   Keys:
 *     latest:status      — full engine status snapshot
 *     latest:pnl         — PnL report
 *     latest:positions   — open positions
 *     latest:trades      — last 50 trades
 *     latest:consensus   — last 20 consensus calls
 *     latest:heartbeat   — {ts, uptime_s, mode}
 *     latest:research    — LLM research top-3
 *     latest:governor    — governor snapshot
 *     latest:llm_health  — per-provider health
 *     latest:universe    — universe top-20
 *     latest:errors      — last 10 errors
 *     history:heartbeat  — JSON array of last 100 heartbeats
 *
 * Env vars (set via wrangler secret):
 *     INGEST_TOKEN       — shared secret for desktop auth
 */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;
    const cors = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Authorization",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }

    // ─── INGEST (desktop → cloud) ───
    if (request.method === "POST" && path === "/ingest") {
      const auth = request.headers.get("Authorization") || "";
      if (auth !== `Bearer ${env.INGEST_TOKEN}`) {
        return json({ error: "unauthorized" }, 401, cors);
      }
      try {
        const payload = await request.json();
        const kind = payload.kind;

        if (kind === "bulk") {
          // BULK MODE: single KV write with ALL state merged.
          // Saves KV writes (free tier = 1000/day).
          const data = payload.data || {};
          data._ingested_at = Date.now();
          await env.APEX_STATE.put("bulk:state", JSON.stringify(data));
          return json({ ok: true, kind: "bulk", ts: Date.now() }, 200, cors);
        }

        if (!kind) return json({ error: "missing 'kind'" }, 400, cors);

        // Legacy per-kind write (for backward compat)
        await env.APEX_STATE.put(`latest:${kind}`, JSON.stringify({
          ...payload.data,
          _ingested_at: Date.now(),
          _kind: kind,
        }));

        return json({ ok: true, kind, ts: Date.now() }, 200, cors);
      } catch (e) {
        return json({ error: e.message }, 400, cors);
      }
    }

    // ─── PUBLIC API (dashboard reads) ───
    if (request.method === "GET" && path.startsWith("/api/")) {
      const key = path.replace("/api/", "").replace(/\//g, ":");

      // Try bulk state first (single KV read, serves all sub-keys)
      const bulk = await env.APEX_STATE.get("bulk:state");
      if (bulk) {
        try {
          const all = JSON.parse(bulk);
          if (all[key]) return json(all[key], 200, cors);
        } catch {}
      }

      // Fallback to per-key lookup (legacy)
      const kvKey = key.includes("history") ? key : `latest:${key}`;
      const val = await env.APEX_STATE.get(kvKey);
      if (!val) return json({ error: "no data yet", key: kvKey }, 404, cors);
      try {
        return json(JSON.parse(val), 200, cors);
      } catch {
        return new Response(val, { status: 200, headers: { ...cors, "Content-Type": "application/json" } });
      }
    }

    // ─── COMMAND QUEUE (cloud dashboard → desktop) ───

    // POST /command — dashboard sends a control command
    if (request.method === "POST" && path === "/command") {
      try {
        const body = await request.json();
        const verb = (body.verb || "").toUpperCase();
        const allowed = ["PAUSE", "RESUME", "HALT", "START", "FLATTEN"];
        if (!allowed.includes(verb)) {
          return json({ error: `invalid verb: ${verb}` }, 400, cors);
        }
        const cmd = {
          id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
          verb,
          reason: body.reason || "cloud dashboard",
          ts: Date.now(),
          status: "pending",
        };
        // Read existing queue
        let queue = [];
        try {
          const raw = await env.APEX_STATE.get("command:queue");
          if (raw) queue = JSON.parse(raw);
        } catch {}
        // Remove stale commands (>5 min old)
        queue = queue.filter(c => Date.now() - c.ts < 300_000);
        queue.push(cmd);
        await env.APEX_STATE.put("command:queue", JSON.stringify(queue));
        return json({ ok: true, command: cmd }, 200, cors);
      } catch (e) {
        return json({ error: e.message }, 400, cors);
      }
    }

    // GET /command/pending — desktop polls for commands to execute
    if (request.method === "GET" && path === "/command/pending") {
      const raw = await env.APEX_STATE.get("command:queue");
      let queue = [];
      try { if (raw) queue = JSON.parse(raw); } catch {}
      const pending = queue.filter(c => c.status === "pending");
      return json({ commands: pending }, 200, cors);
    }

    // POST /command/ack — desktop acknowledges command execution
    if (request.method === "POST" && path === "/command/ack") {
      const auth = request.headers.get("Authorization") || "";
      if (auth !== `Bearer ${env.INGEST_TOKEN}`) {
        return json({ error: "unauthorized" }, 401, cors);
      }
      try {
        const body = await request.json();
        const cmdId = body.id;
        const result = body.result || "ok";
        const raw = await env.APEX_STATE.get("command:queue");
        let queue = [];
        try { if (raw) queue = JSON.parse(raw); } catch {}
        for (const c of queue) {
          if (c.id === cmdId) {
            c.status = "done";
            c.result = result;
            c.ack_ts = Date.now();
          }
        }
        // Keep only last 20 commands for history
        queue = queue.slice(-20);
        await env.APEX_STATE.put("command:queue", JSON.stringify(queue));
        return json({ ok: true }, 200, cors);
      } catch (e) {
        return json({ error: e.message }, 400, cors);
      }
    }

    // GET /command/history — view recent commands
    if (request.method === "GET" && path === "/command/history") {
      const raw = await env.APEX_STATE.get("command:queue");
      let queue = [];
      try { if (raw) queue = JSON.parse(raw); } catch {}
      return json({ commands: queue.slice(-20) }, 200, cors);
    }

    // ─── HEALTH ───
    if (path === "/health") {
      let lastTs = 0;
      // Try bulk first
      const bulk = await env.APEX_STATE.get("bulk:state");
      if (bulk) {
        try {
          const all = JSON.parse(bulk);
          lastTs = all._ingested_at || 0;
        } catch {}
      }
      // Fallback to per-key heartbeat
      if (!lastTs) {
        const hb = await env.APEX_STATE.get("latest:heartbeat");
        if (hb) {
          try {
            const d = JSON.parse(hb);
            lastTs = d._ingested_at || d.ts || 0;
          } catch {}
        }
      }
      const stale = lastTs ? (Date.now() - lastTs) > 180_000 : true; // 3 min (was 1 min)
      return json({
        ok: true,
        desktop_online: !stale,
        last_heartbeat_ms: lastTs,
        stale_seconds: lastTs ? Math.floor((Date.now() - lastTs) / 1000) : -1,
      }, 200, cors);
    }

    // ─── STALE CHECK ───
    if (path === "/stale") {
      const hb = await env.APEX_STATE.get("latest:heartbeat");
      if (!hb) return json({ stale: true, reason: "no heartbeat ever" }, 200, cors);
      try {
        const d = JSON.parse(hb);
        const age = Date.now() - (d._ingested_at || 0);
        return json({ stale: age > 60_000, age_s: Math.floor(age / 1000) }, 200, cors);
      } catch {
        return json({ stale: true, reason: "parse error" }, 200, cors);
      }
    }

    // ─── Fallback: serve dashboard index if Pages isn't separate ───
    if (path === "/" || path === "/index.html") {
      return new Response("APEX-Ω Cloud API. Dashboard at /dashboard/", {
        status: 200, headers: { ...cors, "Content-Type": "text/plain" },
      });
    }

    return json({ error: "not found" }, 404, cors);
  },
};

function json(data, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...extraHeaders },
  });
}
