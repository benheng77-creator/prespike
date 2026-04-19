// OpenClaw Trading Control — UI logic.
//
// Renders /api/state, /api/trading, /api/trading/log into a beginner-first
// hierarchy: status hero on top, glance metrics, current open trade, recent
// closed trades, latest signal, then advanced/diagnostics collapsed below.
//
// Wording rules:
//   - WATCH      → "Monitor only"
//   - NO TRADE   → "No trade taken"
//   - EXECUTE    → "Trade opened"
//   - R          → "risk units (R)"
//   - pWin       → "estimated win chance"
//   - EV-R       → "reward per unit of risk"

const API_BASE = window.location.pathname.replace(/\/$/, "");
const CONN_STATE = document.getElementById("conn-state");
const STATUS_DOT = document.getElementById("hero-status-dot");
const LAST_UPDATED = document.getElementById("last-updated");

let actions = [];
let currentRunId = null;
let lastTradingFetchAt = null;

const ACTION_LABELS = {
  WATCH: "Monitor only",
  "NO TRADE": "No trade taken",
  NO_TRADE: "No trade taken",
  EXECUTE: "Trade opened",
  EXEC: "Trade opened",
  ENTER: "Trade opened",
  REJECT: "Rejected",
  VETO: "Vetoed",
};

document.addEventListener("DOMContentLoaded", () => {
  bindUi();
  bootstrap();
  setInterval(updateLastUpdatedClock, 1000);
});

function bindUi() {
  document.getElementById("btn-refresh").addEventListener("click", refreshAll);
  document.getElementById("btn-copy-tunnel").addEventListener("click", copyTunnel);
  document.getElementById("btn-quit").addEventListener("click", quit);
  document.getElementById("btn-run").addEventListener("click", runSelected);
  document.getElementById("action-select").addEventListener("change", updateDescription);

  document.getElementById("hero-secondary").addEventListener("click", refreshAll);
  document.getElementById("hero-primary").addEventListener("click", () => {
    const action = document.getElementById("hero-primary").dataset.action;
    if (!action) {
      return;
    }
    if (action === "retry") {
      refreshAll();
      return;
    }
    if (action === "trading_halt") {
      if (!confirm("Pause trading? The bot will not open any new trades until you resume it.")) {
        return;
      }
    }
    triggerRun(action);
    setTimeout(refreshTrading, 800);
  });

  const tradingBindings = {
    "btn-trading-config": "trading_view_config",
    "btn-trading-tail": "trading_tail_log",
    "btn-trading-pyprocs": "trading_python_processes",
  };
  for (const [btnId, actionId] of Object.entries(tradingBindings)) {
    const btn = document.getElementById(btnId);
    if (btn) {
      btn.addEventListener("click", () => {
        triggerRun(actionId);
      });
    }
  }
}

async function bootstrap() {
  await refreshAll();
  openEventStream();
  setInterval(refreshTrading, 4000);
  bindSettingsLazyLoad();
}

async function refreshAll() {
  await Promise.all([refresh(), refreshTrading()]);
}

async function refresh() {
  try {
    const state = await api("/api/state");
    renderSystemState(state);
  } catch (err) {
    console.warn("system refresh failed", err);
  }
}

function renderProver(prover) {
  const summaryEl = document.getElementById("launch-prover-summary");
  const bodyEl = document.getElementById("launch-prover-body");
  if (!summaryEl || !bodyEl) {
    return;
  }
  if (!prover) {
    bodyEl.innerHTML = "";
    return;
  }
  if (prover.error) {
    summaryEl.textContent = `prover error: ${prover.error}`;
    bodyEl.innerHTML = "";
    return;
  }
  const earned = prover.earned_live_symbols || [];
  const ts = prover.generated_at_ms
    ? new Date(Number(prover.generated_at_ms)).toLocaleString()
    : "—";
  const earnedNames = earned
    .map((e) => (typeof e === "string" ? e : `${e.symbol}(${e.best_level})`))
    .join(", ");
  summaryEl.innerHTML =
    earned.length > 0
      ? `<strong style="color: var(--ok)">${earned.length} symbol(s) earned live</strong>: ${escapeHtml(earnedNames)} · run at ${escapeHtml(ts)}`
      : `0 of ${prover.results?.length || 0} symbols hit ${(prover.accuracy_floor || 0.75) * 100}% floor · run at ${escapeHtml(ts)}`;

  const results = prover.results || [];
  if (results.length === 0) {
    bodyEl.innerHTML = "";
    return;
  }
  bodyEl.innerHTML = results
    .map((r) => {
      if (r.error) {
        return `<div class="launch-prover-cell">
          <div class="sym">${escapeHtml(r.symbol)}</div>
          <div class="lvl thin">${escapeHtml(r.error)}</div>
        </div>`;
      }
      const earnedClass = r.earned_live ? " earned" : "";
      const levels = (r.levels || [])
        .map((lvl) => {
          const cls = lvl.earned_live
            ? "lvl earned"
            : lvl.trades < (prover.min_trades_for_verdict || 10)
              ? "lvl thin"
              : "lvl";
          return `<span class="${cls}" title="${escapeHtml(lvl.verdict)}">${escapeHtml(lvl.level)}: ${(lvl.win_rate * 100).toFixed(0)}%</span>`;
        })
        .join("");
      return `<div class="launch-prover-cell${earnedClass}">
        <div class="sym">${escapeHtml(r.symbol)}</div>
        <div class="levels">${levels}</div>
      </div>`;
    })
    .join("");
}

async function refreshTrading() {
  try {
    const [state, log, launch] = await Promise.all([
      api("/api/trading"),
      api("/api/trading/log"),
      api("/api/launch").catch(() => null),
    ]);
    lastTradingFetchAt = new Date();
    renderTradingState(state);
    renderTradingLog(log);
    if (launch) {
      renderLaunch(launch);
    }
    updateLastUpdatedClock();
  } catch (err) {
    console.warn("trading refresh failed", err);
  }
}

function renderLaunch(launch) {
  if (!launch) {
    return;
  }

  renderProver(launch.prover);

  // --- Live gates column ---
  const gates = (launch.executor && launch.executor.gates) || [];
  const listEl = document.getElementById("launch-gates");
  if (listEl) {
    if (gates.length === 0) {
      listEl.innerHTML = '<li class="muted">no gate data</li>';
    } else {
      listEl.innerHTML = gates
        .map(
          (g) => `
          <li class="${g.open ? "open" : "closed"}">
            <span class="launch-gate-icon"></span>
            <div>
              <div class="launch-gate-label">${escapeHtml(g.label)}</div>
              <div class="launch-gate-detail">${escapeHtml(g.detail || "")}</div>
            </div>
            <span class="chip ${g.open ? "green" : "red"}">${g.open ? "OPEN" : "CLOSED"}</span>
          </li>`,
        )
        .join("");
    }
  }
  const countEl = document.getElementById("launch-gate-count");
  if (countEl) {
    const openCount = Number(launch.executor?.open_count ?? 0);
    const total = Number(launch.executor?.total ?? 4);
    countEl.textContent = `${openCount} / ${total}`;
    countEl.className =
      "launch-col-score " +
      (openCount === total ? "all-green" : openCount > 0 ? "partial" : "none");
  }

  // --- Accuracy gate column ---
  const accSymbols = (launch.accuracy && launch.accuracy.symbols) || [];
  const floorPct = launch.accuracy?.config?.floor_pct ?? 0.75;
  const minTrades = launch.accuracy?.config?.min_trades_before_floor ?? 10;
  const accBody = document.getElementById("launch-accuracy-body");
  if (accBody) {
    if (accSymbols.length === 0) {
      accBody.innerHTML = '<tr><td colspan="4" class="muted">no data</td></tr>';
    } else {
      accBody.innerHTML = accSymbols
        .map((row) => {
          if (row.error) {
            return `<tr>
              <td>${escapeHtml(row.symbol)}</td>
              <td colspan="3" class="muted">${escapeHtml(row.error)}</td>
            </tr>`;
          }
          const wrPct = (Number(row.rolling_wr || 0) * 100).toFixed(0);
          const enoughSamples = Number(row.window_trades || 0) >= minTrades;
          const cls = !enoughSamples ? "ns" : row.rolling_wr >= floorPct ? "pass" : "fail";
          const statusChip = row.live_allowed
            ? '<span class="chip green">live ok</span>'
            : row.paper_allowed
              ? '<span class="chip yellow">paper only</span>'
              : '<span class="chip red">vetoed</span>';
          return `<tr>
            <td>${escapeHtml(row.symbol)}</td>
            <td class="wr-cell ${cls}">${wrPct}%</td>
            <td>${row.window_trades}</td>
            <td>${statusChip}</td>
          </tr>`;
        })
        .join("");
    }
  }
  const accSummary = document.getElementById("launch-accuracy-summary");
  if (accSummary) {
    const passing = accSymbols.filter(
      (s) => !s.error && s.window_trades >= minTrades && s.rolling_wr >= floorPct,
    ).length;
    const total = accSymbols.length;
    accSummary.textContent = total ? `${passing} / ${total}` : "—";
    accSummary.className =
      "launch-col-score " +
      (total === 0 ? "none" : passing === total ? "all-green" : passing > 0 ? "partial" : "none");
  }

  // --- Scanner column ---
  const scannerBody = document.getElementById("launch-scanner-body");
  const scannerTs = document.getElementById("launch-scanner-ts");
  const scannerHint = document.getElementById("launch-scanner-hint");
  const ranking = launch.scanner?.ranking;
  if (scannerBody) {
    if (!ranking || !ranking.ranking || ranking.ranking.length === 0) {
      scannerBody.innerHTML = '<tr><td colspan="4" class="muted">scanner not running yet</td></tr>';
      if (scannerTs) {
        scannerTs.textContent = "—";
        scannerTs.className = "launch-col-score none";
      }
      if (scannerHint) {
        if (launch.scanner?.config?.enabled) {
          scannerHint.textContent =
            "scanner enabled but has not written a ranking yet; waiting for first cycle…";
        } else {
          scannerHint.innerHTML =
            "Enable <code>scanner.enabled: true</code> in openclaw.yaml and restart the bot to populate this column.";
        }
      }
    } else {
      const sorted = ranking.ranking.toSorted(
        (a, b) =>
          Number(b.ScoreTotal || 0) * Math.max(0, Number(b.EV_R || 0)) -
          Number(a.ScoreTotal || 0) * Math.max(0, Number(a.EV_R || 0)),
      );
      scannerBody.innerHTML = sorted
        .slice(0, 6)
        .map((row, idx) => {
          const action = row.action || "?";
          const actionChip =
            action === "EXECUTE"
              ? '<span class="chip green">EXEC</span>'
              : action === "WATCH"
                ? '<span class="chip blue">WATCH</span>'
                : '<span class="chip grey">SKIP</span>';
          return `<tr>
            <td>${idx + 1}</td>
            <td>${escapeHtml(row.symbol)}</td>
            <td>${Number(row.ScoreTotal || 0).toFixed(1)}</td>
            <td>${actionChip}</td>
          </tr>`;
        })
        .join("");
      if (scannerTs) {
        const ts = ranking.generated_at_ms
          ? new Date(Number(ranking.generated_at_ms)).toLocaleTimeString()
          : "—";
        scannerTs.textContent = ts;
        scannerTs.className = "launch-col-score all-green";
      }
      if (scannerHint) {
        scannerHint.textContent = `${ranking.ranking.length} symbols ranked · ${ranking.tradable_symbols?.length || 0} tradable this cycle`;
      }
    }
  }
}

function api(path, init) {
  return fetch(`${API_BASE}${path}`, {
    headers: { accept: "application/json" },
    ...init,
  }).then(async (res) => {
    if (!res.ok) {
      throw new Error(`${res.status} ${res.statusText}`);
    }
    return res.json();
  });
}

// ============================================================
//  System / diagnostics rendering
// ============================================================

function renderSystemState(state) {
  if (!state) {
    return;
  }
  const panel = state.panel || {};
  setText("sys-host", panel.hostname);
  setText("sys-platform", panel.platform);
  setText("sys-node", panel.nodeVersion);
  setText("sys-uptime", formatDuration(panel.uptimeSeconds * 1000));
  setText("sys-cpu", panel.cpuCount);
  setText(
    "sys-load",
    Array.isArray(panel.loadAverage) ? panel.loadAverage.map((x) => x.toFixed(2)).join(" / ") : "—",
  );
  setText("sys-mem-total", `${panel.memory && panel.memory.totalMb} MB`);
  setText("sys-mem-free", `${panel.memory && panel.memory.freeMb} MB`);
  setText("sys-mem-rss", `${panel.memory && panel.memory.rssMb} MB`);
  setText("sys-project", panel.projectRoot || "—");

  renderTunnel(state.tunnel || {});
  renderActions(state.actions || []);
  renderRecentRuns(state.recentRuns || []);
}

function renderTunnel(tunnel) {
  const input = document.getElementById("tunnel-url");
  if (tunnel.panelUrl) {
    input.value = tunnel.panelUrl;
  } else if (tunnel.base) {
    input.value = tunnel.base;
  } else {
    input.value = "(starting…)";
  }
}

function renderActions(list) {
  if (!Array.isArray(list)) {
    return;
  }
  actions = list;
  const select = document.getElementById("action-select");
  if (select.options.length !== list.length) {
    select.innerHTML = "";
    for (const action of list) {
      const opt = document.createElement("option");
      opt.value = action.id;
      opt.textContent = action.label;
      select.appendChild(opt);
    }
  }
  updateDescription();
}

function updateDescription() {
  const select = document.getElementById("action-select");
  const id = select.value;
  const action = actions.find((a) => a.id === id);
  document.getElementById("action-description").textContent = (action && action.description) || "";
}

function renderRecentRuns(runs) {
  const tbody = document.getElementById("runs-body");
  if (!runs.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="muted">no runs yet</td></tr>';
    return;
  }
  tbody.innerHTML = runs
    .map(
      (r) => `
      <tr>
        <td>${escapeHtml(formatTime(r.startedAt))}</td>
        <td>${escapeHtml(r.label || r.actionId)}</td>
        <td><span class="badge" data-status="${escapeHtml(r.status)}">${escapeHtml(r.status)}</span></td>
        <td>${r.exitCode ?? "—"}</td>
        <td>${r.durationMs != null ? `${r.durationMs} ms` : "—"}</td>
      </tr>`,
    )
    .join("");
}

// ============================================================
//  Trading state rendering — the beginner-first hierarchy
// ============================================================

function renderTradingState(state) {
  if (!state) {
    return;
  }
  renderHero(state);
  renderGlance(state);
  renderOpenPosition(state);
  renderRecentTrades(state);
  renderLatestSignal(state);
  renderDailyPnl(state.daily_pnl_history || []);
  renderRecentDecisionsRaw(state.recent_decisions || []);
  renderEngineDiagnostics(state);
}

function renderHero(state) {
  const hero = document.getElementById("hero");
  const icon = document.getElementById("hero-icon");
  const title = document.getElementById("hero-title");
  const message = document.getElementById("hero-message");
  const primary = document.getElementById("hero-primary");
  const secondary = document.getElementById("hero-secondary");

  const openCount = (state.open_trades || []).length;
  const openSummary =
    openCount === 0
      ? "There are no open trades right now."
      : openCount === 1
        ? `There is 1 open ${state.open_trades[0].symbol} trade still being monitored.`
        : `There are ${openCount} open trades still being monitored.`;

  if (state.error) {
    hero.dataset.state = "error";
    icon.textContent = "⚠";
    title.textContent = "Trading state unknown";
    message.textContent = `The panel could not read the trading database. ${state.error}`;
    primary.textContent = "Retry";
    primary.dataset.action = "retry";
    secondary.hidden = true;
    STATUS_DOT.dataset.state = "error";
    return;
  }

  secondary.hidden = false;

  if (state.halted) {
    hero.dataset.state = "paused";
    icon.textContent = "⏸";
    title.textContent = "Trading paused";
    message.textContent = `The system is not opening new trades because manual halt is enabled. ${openSummary}`;
    primary.textContent = "▶ Resume trading";
    primary.dataset.action = "trading_resume";
    primary.classList.remove("danger");
    primary.classList.add("primary");
    STATUS_DOT.dataset.state = "paused";
  } else {
    hero.dataset.state = "active";
    icon.textContent = "▶";
    title.textContent = "Trading active";
    message.textContent = `The bot is running and free to open new trades. ${openSummary}`;
    primary.textContent = "⏸ Pause trading";
    primary.dataset.action = "trading_halt";
    primary.classList.remove("primary");
    primary.classList.add("danger");
    STATUS_DOT.dataset.state = "active";
  }
}

function renderGlance(state) {
  const today = state.today_pnl || {};
  const closed = Number(today.closed_count || 0);
  const wins = Number(today.wins || 0);
  const losses = Number(today.losses || 0);

  const pnlEl = document.getElementById("glance-pnl-quote");
  pnlEl.textContent = formatMoney(today.total_quote);
  pnlEl.className = `glance-value ${signClass(today.total_quote)}`;

  setText("glance-closed", closed);
  setText("glance-winrate", closed > 0 ? `${((wins / closed) * 100).toFixed(0)}%` : "—");
  setText("glance-winloss", `${wins} / ${losses}`);

  const bestEl = document.getElementById("glance-best");
  bestEl.textContent = closed > 0 ? `${formatSigned(today.best_r)} R` : "—";
  bestEl.className = `glance-value ${signClass(today.best_r)}`;

  const worstEl = document.getElementById("glance-worst");
  worstEl.textContent = closed > 0 ? `${formatSigned(today.worst_r)} R` : "—";
  worstEl.className = `glance-value ${signClass(today.worst_r)}`;
}

function renderOpenPosition(state) {
  const trades = state.open_trades || [];
  const intro = document.getElementById("open-intro");
  const empty = document.getElementById("open-empty");
  const detail = document.getElementById("open-detail");

  if (!trades.length) {
    intro.textContent = "No open positions right now.";
    empty.hidden = false;
    detail.hidden = true;
    return;
  }

  const t = trades[0];
  intro.textContent =
    trades.length === 1
      ? "One trade is currently active. The system is watching for its stop or target."
      : `${trades.length} trades are currently active. Showing the most recent.`;
  empty.hidden = true;
  detail.hidden = false;

  setText("open-symbol", t.symbol);
  setText(
    "open-direction",
    t.direction === 1 ? "Long (betting price will rise)" : "Short (betting price will fall)",
  );
  setText("open-size", formatNumber(t.size, 6));
  setText("open-entry", formatNumber(t.entry_px));
  setText("open-stop", formatNumber(t.stop_px));
  setText("open-target", formatNumber(t.target_px));
}

function renderRecentTrades(state) {
  const tbody = document.getElementById("recent-trades-body");
  const todayTrades = (state.today_trades || []).filter((t) => t.status !== "open");
  const closed = todayTrades.length > 0 ? todayTrades : state.recent_decisions ? [] : [];

  if (!closed.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="muted">no closed trades today</td></tr>';
    return;
  }

  tbody.innerHTML = closed
    .map((t) => {
      const tsMs = Number(t.exit_ts_ms || t.entry_ts_ms);
      const time = Number.isFinite(tsMs) ? new Date(tsMs).toLocaleTimeString() : "—";
      const dir = t.direction === 1 ? "Long" : "Short";
      const pnlR = formatSigned(t.pnl_r);
      const pnlQ = formatMoney(t.pnl_quote);
      const isWin = Number(t.pnl_r) > 0;
      const cls = signClass(t.pnl_r);
      return `
        <tr>
          <td>${escapeHtml(time)}</td>
          <td>${escapeHtml(t.symbol)}</td>
          <td>${dir}</td>
          <td class="${cls}"><strong>${pnlQ}</strong> <span class="muted-mono">(${pnlR}R)</span></td>
          <td><span class="chip ${isWin ? "green" : "red"}">${isWin ? "Win" : "Loss"}</span></td>
        </tr>`;
    })
    .join("");
}

function renderLatestSignal(state) {
  const d = state.latest_decision;
  if (!d) {
    setText("signal-symbol", "—");
    setText("signal-action", "—");
    setText("signal-score", "—");
    setText("signal-pwin", "—");
    setText("signal-evr", "—");
    return;
  }
  setText("signal-symbol", d.symbol || "—");
  setText("signal-action", humanAction(d.action));
  setText("signal-score", d.score_total != null ? `${formatNumber(d.score_total)} / 100` : "—");
  setText("signal-pwin", d.pwin_pct != null ? `${formatNumber(d.pwin_pct)}%` : "—");
  setText("signal-evr", d.ev_r != null ? `${formatNumber(d.ev_r)}×` : "—");

  const breakdown = state.today_decisions_breakdown || [];
  if (breakdown.length === 0) {
    setText("signal-meta", "No market checks completed today yet.");
    return;
  }
  const total = breakdown.reduce((sum, row) => sum + Number(row.n || 0), 0);
  const parts = breakdown
    .map((row) => `${humanAction(row.action)}: ${Number(row.n).toLocaleString()}`)
    .join("  ·  ");
  document.getElementById("signal-meta").textContent =
    `Market checks completed today: ${total.toLocaleString()}  →  ${parts}`;
}

function renderDailyPnl(days) {
  const tbody = document.getElementById("daily-pnl-body");
  if (!days.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="muted">no closed trades yet</td></tr>';
    return;
  }
  tbody.innerHTML = days
    .map((d) => {
      const wr = d.count > 0 ? `${((Number(d.wins) / Number(d.count)) * 100).toFixed(0)}%` : "—";
      const qCls = signClass(d.total_quote);
      return `
        <tr>
          <td>${escapeHtml(d.day)}</td>
          <td>${d.count}</td>
          <td class="pos">${d.wins}</td>
          <td class="neg">${d.losses}</td>
          <td>${wr}</td>
          <td class="${qCls}"><strong>${formatMoney(d.total_quote)}</strong></td>
        </tr>`;
    })
    .join("");
}

function renderRecentDecisionsRaw(decisions) {
  const tbody = document.getElementById("trading-decisions-body");
  if (!decisions.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="muted">no decisions yet</td></tr>';
    return;
  }
  tbody.innerHTML = decisions
    .map((d) => {
      const ts = d.created_at ? new Date(Number(d.created_at)).toLocaleTimeString() : "—";
      return `
      <tr>
        <td>${escapeHtml(ts)}</td>
        <td>${escapeHtml(d.symbol || "?")}</td>
        <td><span class="badge" data-status="${decisionStatus(d.action)}">${escapeHtml(d.action || "?")}</span></td>
        <td>${formatNumber(d.score_total)}</td>
        <td>${formatNumber(d.pwin_pct)}%</td>
        <td>${formatNumber(d.ev_r)}</td>
      </tr>`;
    })
    .join("");
}

function renderEngineDiagnostics(state) {
  setText(
    "diag-engine",
    state.pythonProcesses == null
      ? "unknown"
      : state.pythonProcesses > 0
        ? `running (${state.pythonProcesses} python processes)`
        : "not running",
  );
  setText("diag-halt", state.halted ? "Halt file present (paused)" : "No halt file (active)");
  setText(
    "diag-decision-count",
    state.decision_count != null ? state.decision_count.toLocaleString() : "—",
  );
  setText("diag-trade-count", state.trade_count != null ? state.trade_count.toLocaleString() : "—");
  setText("diag-db", state.tradingDb || "—");
  setText("diag-db-mtime", state.dbModifiedAt ? formatTime(state.dbModifiedAt) : "—");
}

function renderTradingLog(payload) {
  const pre = document.getElementById("trading-log-tail");
  if (!pre) {
    return;
  }
  if (!payload || !Array.isArray(payload.lines)) {
    pre.textContent = "(no data)";
    return;
  }
  const lines = payload.lines.filter((l) => l && l.length > 0).slice(-60);
  pre.textContent = lines.join("\n");
  pre.scrollTop = pre.scrollHeight;
}

// ============================================================
//  Action runner (used by Advanced and the hero primary button)
// ============================================================

function runSelected() {
  const select = document.getElementById("action-select");
  triggerRun(select.value);
}

function triggerRun(actionId) {
  if (!actionId) {
    return;
  }
  document.getElementById("run-output").textContent = "";
  document.getElementById("run-status").textContent = "running";
  document.getElementById("run-status").dataset.status = "running";
  document.getElementById("run-exit").textContent = "—";
  document.getElementById("run-duration").textContent = "—";
  const action = actions.find((a) => a.id === actionId);
  document.getElementById("run-label").textContent = (action && action.label) || actionId;

  api("/api/run", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({ id: actionId }),
  }).catch((err) => {
    document.getElementById("run-status").textContent = "error";
    document.getElementById("run-status").dataset.status = "error";
    document.getElementById("run-output").textContent = String(err);
  });
}

async function copyTunnel() {
  const input = document.getElementById("tunnel-url");
  if (!input.value || input.value.startsWith("(")) {
    return;
  }
  try {
    await navigator.clipboard.writeText(input.value);
    flash(document.getElementById("btn-copy-tunnel"), "Copied!");
  } catch {
    input.select();
    document.execCommand("copy");
    flash(document.getElementById("btn-copy-tunnel"), "Copied");
  }
}

function quit() {
  if (!confirm("Stop the panel? It will only restart if auto-launch is enabled.")) {
    return;
  }
  api("/api/quit", { method: "POST" }).catch(() => {});
}

// ============================================================
//  Server-Sent Events
// ============================================================

function openEventStream() {
  CONN_STATE.dataset.state = "connecting";
  CONN_STATE.textContent = "connecting…";
  const es = new EventSource(`${API_BASE}/api/events`);
  es.addEventListener("open", () => {
    CONN_STATE.dataset.state = "connected";
    CONN_STATE.textContent = "connected";
  });
  es.addEventListener("error", () => {
    CONN_STATE.dataset.state = "lost";
    CONN_STATE.textContent = "reconnecting…";
  });
  es.addEventListener("message", (msg) => {
    if (!msg.data) {
      return;
    }
    let event;
    try {
      event = JSON.parse(msg.data);
    } catch {
      return;
    }
    handleEvent(event);
  });
}

function handleEvent(event) {
  if (event.type === "hello" && event.state) {
    renderSystemState(event.state);
    return;
  }
  if (event.type === "run.start" && event.run) {
    currentRunId = event.run.runId;
    document.getElementById("run-label").textContent = event.run.label || event.run.actionId;
    document.getElementById("run-status").textContent = "running";
    document.getElementById("run-status").dataset.status = "running";
    document.getElementById("run-output").textContent = "";
    return;
  }
  if (event.type === "run.output" && event.runId === currentRunId) {
    const out = document.getElementById("run-output");
    out.textContent += event.text;
    out.scrollTop = out.scrollHeight;
    return;
  }
  if (event.type === "run.end" && event.run) {
    if (event.run.runId === currentRunId) {
      document.getElementById("run-status").textContent = event.run.status;
      document.getElementById("run-status").dataset.status = event.run.status;
      document.getElementById("run-exit").textContent = event.run.exitCode ?? "—";
      document.getElementById("run-duration").textContent =
        event.run.durationMs != null ? `${event.run.durationMs} ms` : "—";
    }
    refresh();
    refreshTrading();
  }
}

// ============================================================
//  Helpers
// ============================================================

function humanAction(action) {
  if (!action) {
    return "—";
  }
  return ACTION_LABELS[action] || ACTION_LABELS[action.toUpperCase()] || action;
}

function decisionStatus(action) {
  if (!action) {
    return "idle";
  }
  const upper = action.toUpperCase();
  if (upper === "EXEC" || upper === "EXECUTE" || upper === "ENTER") {
    return "running";
  }
  if (upper === "WATCH") {
    return "idle";
  }
  if (upper === "REJECT" || upper === "VETO") {
    return "fail";
  }
  return "idle";
}

function signClass(value) {
  if (value == null || Number.isNaN(Number(value))) {
    return "";
  }
  const n = Number(value);
  if (n > 0) {
    return "pos";
  }
  if (n < 0) {
    return "neg";
  }
  return "";
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) {
    el.textContent = value == null || value === "" ? "—" : String(value);
  }
}

function formatDuration(ms) {
  if (!ms || Number.isNaN(ms)) {
    return "—";
  }
  const sec = Math.round(ms / 1000);
  if (sec < 60) {
    return `${sec}s`;
  }
  const min = Math.floor(sec / 60);
  const rest = sec % 60;
  if (min < 60) {
    return `${min}m ${rest}s`;
  }
  const hours = Math.floor(min / 60);
  return `${hours}h ${min % 60}m`;
}

function formatTime(iso) {
  if (!iso) {
    return "—";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return date.toLocaleTimeString();
}

function flash(button, label) {
  const original = button.textContent;
  button.textContent = label;
  setTimeout(() => {
    button.textContent = original;
  }, 1200);
}

function escapeHtml(value) {
  if (value == null) {
    return "";
  }
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatNumber(value, decimals = 2) {
  if (value == null || value === "" || Number.isNaN(Number(value))) {
    return "—";
  }
  return Number(value).toFixed(decimals);
}

function formatSigned(value) {
  if (value == null || Number.isNaN(Number(value))) {
    return "—";
  }
  const n = Number(value);
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}`;
}

function formatMoney(value) {
  if (value == null || Number.isNaN(Number(value))) {
    return "—";
  }
  const n = Number(value);
  if (n === 0) {
    return "$0.00";
  }
  const sign = n > 0 ? "+$" : "-$";
  return `${sign}${Math.abs(n).toFixed(2)}`;
}

// ============================================================
//  Settings — API keys & integrations
// ============================================================

let settingsLoaded = false;

function bindSettingsLazyLoad() {
  const card = document.getElementById("settings-card");
  if (!card) {
    return;
  }
  card.addEventListener("toggle", () => {
    if (card.open && !settingsLoaded) {
      loadSettings();
    }
  });
}

async function loadSettings() {
  const groupsEl = document.getElementById("settings-groups");
  const metaEl = document.getElementById("settings-files-meta");
  groupsEl.innerHTML = '<p class="muted">loading…</p>';
  try {
    const data = await api("/api/secrets");
    settingsLoaded = true;
    renderSettings(data, groupsEl, metaEl);
  } catch (err) {
    groupsEl.innerHTML = `<p class="muted">failed to load: ${escapeHtml(String(err))}</p>`;
  }
}

function renderSettings(data, groupsEl, metaEl) {
  // Files meta block
  metaEl.innerHTML = "";
  for (const meta of Object.values(data.files || {})) {
    const line = document.createElement("div");
    line.className = "file-line";
    const status = meta.exists ? `${meta.keysOnDisk} keys` : "(file does not exist yet)";
    line.innerHTML = `
      <strong>${escapeHtml(meta.label)}</strong>
      <code>${escapeHtml(meta.path)}</code>
      <span>${escapeHtml(status)}</span>
    `;
    metaEl.appendChild(line);
  }

  // Group by file then by category
  const byFile = new Map();
  for (const group of data.groups || []) {
    if (!byFile.has(group.file)) {
      byFile.set(group.file, []);
    }
    byFile.get(group.file).push(group);
  }

  groupsEl.innerHTML = "";
  for (const [fileId, groups] of byFile.entries()) {
    const fileWrap = document.createElement("div");
    fileWrap.className = "settings-file";
    const fileLabel = data.files?.[fileId]?.label || fileId;
    fileWrap.innerHTML = `<h3>${escapeHtml(fileLabel)}</h3>`;
    for (const group of groups) {
      const catTitle = document.createElement("div");
      catTitle.className = "settings-category";
      catTitle.textContent = group.category;
      fileWrap.appendChild(catTitle);
      for (const k of group.keys) {
        fileWrap.appendChild(buildSettingsRow(fileId, k));
      }
    }
    groupsEl.appendChild(fileWrap);
  }
}

function buildSettingsRow(fileId, key) {
  const row = document.createElement("div");
  row.className = "settings-row";
  row.dataset.file = fileId;
  row.dataset.name = key.name;

  const info = document.createElement("div");
  info.className = "settings-key-info";
  const setChip = key.set
    ? '<span class="settings-set-chip">SET</span>'
    : '<span class="settings-empty-chip">empty</span>';
  info.innerHTML = `
    <div class="settings-key-name">${escapeHtml(key.label || key.name)} ${setChip}</div>
    <div class="settings-key-meta">${escapeHtml(key.name)}${key.preview ? " &middot; " + escapeHtml(key.preview) : ""}</div>
    ${key.hint ? `<div class="settings-key-hint">${escapeHtml(key.hint)}</div>` : ""}
  `;

  const input = document.createElement("input");
  input.type = "password";
  input.placeholder = key.set ? "(leave blank to keep current value)" : "paste your key here";
  input.autocomplete = "off";
  input.spellcheck = false;

  const actions = document.createElement("div");
  actions.className = "settings-actions";

  const showBtn = document.createElement("button");
  showBtn.type = "button";
  showBtn.textContent = "Show";
  showBtn.title = "Toggle visibility while typing";
  showBtn.addEventListener("click", () => {
    if (input.type === "password") {
      input.type = "text";
      showBtn.textContent = "Hide";
    } else {
      input.type = "password";
      showBtn.textContent = "Show";
    }
  });

  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.textContent = "Save";
  saveBtn.className = "primary";

  const status = document.createElement("span");
  status.className = "settings-status";

  saveBtn.addEventListener("click", async () => {
    const value = input.value;
    if (!value) {
      status.textContent = "type a value first";
      status.className = "settings-status err";
      return;
    }
    saveBtn.disabled = true;
    status.textContent = "saving…";
    status.className = "settings-status";
    try {
      const res = await api("/api/secrets", {
        method: "POST",
        headers: { "content-type": "application/json", accept: "application/json" },
        body: JSON.stringify({ file: fileId, name: key.name, value }),
      });
      if (res.ok) {
        status.textContent = "saved ✓";
        status.className = "settings-status ok";
        input.value = "";
        input.placeholder = "(leave blank to keep current value)";
        // Update the displayed meta line for this row
        const meta = info.querySelector(".settings-key-meta");
        if (meta) {
          meta.innerHTML = `${escapeHtml(key.name)} &middot; ${escapeHtml(res.preview || "")}`;
        }
        const chipHost = info.querySelector(".settings-key-name");
        if (chipHost) {
          const chip = chipHost.querySelector(".settings-set-chip, .settings-empty-chip");
          if (chip) {
            chip.outerHTML = '<span class="settings-set-chip">SET</span>';
          }
        }
      } else {
        status.textContent = res.error || "failed";
        status.className = "settings-status err";
      }
    } catch (err) {
      status.textContent = String(err);
      status.className = "settings-status err";
    } finally {
      saveBtn.disabled = false;
    }
  });

  const clearBtn = document.createElement("button");
  clearBtn.type = "button";
  clearBtn.textContent = "Delete";
  clearBtn.className = "danger";
  clearBtn.title = "Remove this key from the .env file";
  clearBtn.addEventListener("click", async () => {
    if (!confirm(`Delete ${key.name} from the .env file?`)) {
      return;
    }
    clearBtn.disabled = true;
    status.textContent = "deleting…";
    try {
      const res = await api("/api/secrets", {
        method: "POST",
        headers: { "content-type": "application/json", accept: "application/json" },
        body: JSON.stringify({ file: fileId, name: key.name, value: "" }),
      });
      if (res.ok) {
        status.textContent = "deleted ✓";
        status.className = "settings-status ok";
        input.value = "";
        input.placeholder = "paste your key here";
        const meta = info.querySelector(".settings-key-meta");
        if (meta) {
          meta.textContent = key.name;
        }
        const chipHost = info.querySelector(".settings-key-name");
        if (chipHost) {
          const chip = chipHost.querySelector(".settings-set-chip, .settings-empty-chip");
          if (chip) {
            chip.outerHTML = '<span class="settings-empty-chip">empty</span>';
          }
        }
      } else {
        status.textContent = res.error || "failed";
        status.className = "settings-status err";
      }
    } catch (err) {
      status.textContent = String(err);
      status.className = "settings-status err";
    } finally {
      clearBtn.disabled = false;
    }
  });

  actions.appendChild(showBtn);
  actions.appendChild(saveBtn);
  actions.appendChild(clearBtn);
  actions.appendChild(status);

  row.appendChild(info);
  row.appendChild(input);
  row.appendChild(actions);
  return row;
}

function updateLastUpdatedClock() {
  if (!LAST_UPDATED) {
    return;
  }
  if (!lastTradingFetchAt) {
    LAST_UPDATED.textContent = "updated …";
    return;
  }
  const seconds = Math.max(0, Math.round((Date.now() - lastTradingFetchAt.getTime()) / 1000));
  if (seconds < 5) {
    LAST_UPDATED.textContent = "updated just now";
  } else if (seconds < 60) {
    LAST_UPDATED.textContent = `updated ${seconds}s ago`;
  } else {
    LAST_UPDATED.textContent = `updated ${Math.round(seconds / 60)} min ago`;
  }
}
