"""Phase 11n-9-ll — Daily 24h Governance Report.

Generates the 9-section 1-page executive report. Runs every 24h via
server.py daemon. Also callable on-demand via /gov/daily_report/run.

Persists JSON + markdown to:
  spot_daily_reports table (JSON, SQLite)
  reports/daily_YYYY-MM-DD.md (markdown snapshot, disk)

Every daily report is strictly compressed — high-signal only.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _db_path() -> str:
    return (
        os.environ.get("TRADE_DB_PATH")
        or os.environ.get("CLAW_DB_PATH")
        or "trades.db"
    )


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path(), isolation_level=None, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def _init_schema() -> None:
    con = _connect()
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS spot_daily_reports("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts_ms INTEGER NOT NULL,"
            " report_date TEXT NOT NULL,"
            " verdict TEXT NOT NULL,"
            " headline TEXT,"
            " payload_json TEXT NOT NULL,"
            " markdown TEXT"
            ")"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_dr_date "
            "ON spot_daily_reports(report_date DESC)"
        )
    finally:
        con.close()


WINDOW_H = 24


@dataclass
class DailyReport:
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    report_date: str = ""
    verdict: str = ""            # 'improving' | 'degrading' | 'flat' | 'insufficient'
    headline: str = ""
    executive_status: dict[str, Any] = field(default_factory=dict)
    activity: dict[str, Any] = field(default_factory=dict)
    performance: dict[str, Any] = field(default_factory=dict)
    critical_gaps: list[str] = field(default_factory=list)
    root_cause: str = ""
    governance_decision: dict[str, Any] = field(default_factory=dict)
    upgrade_focus: str = ""
    strategy_progress: dict[str, Any] = field(default_factory=dict)
    markdown: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fetch_trades_24h() -> list[sqlite3.Row]:
    cutoff = int(time.time() * 1000) - WINDOW_H * 3600 * 1000
    con = _connect()
    try:
        return con.execute(
            "SELECT ts_ms, symbol, action, tier, notional_usd, pnl_usd,"
            " fee_usd, payload_json FROM trade_log"
            " WHERE ts_ms >= ? ORDER BY ts_ms ASC",
            (cutoff,),
        ).fetchall()
    finally:
        con.close()


def _fetch_pretrade_24h() -> list[sqlite3.Row]:
    cutoff = int(time.time() * 1000) - WINDOW_H * 3600 * 1000
    try:
        con = _connect()
        try:
            return con.execute(
                "SELECT ts_ms, symbol, tier, passed, rejection"
                " FROM spot_pre_trade_authorizations"
                " WHERE ts_ms >= ? ORDER BY ts_ms ASC",
                (cutoff,),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.OperationalError:
        # Table may not exist in a fresh DB; treat as zero activity.
        return []


def _fetch_kill_ladder_events_24h() -> list[sqlite3.Row]:
    cutoff = int(time.time() * 1000) - WINDOW_H * 3600 * 1000
    try:
        con = _connect()
        try:
            return con.execute(
                "SELECT ts_ms, level, reason, actor"
                " FROM spot_kill_ladder_state"
                " WHERE ts_ms >= ? ORDER BY ts_ms ASC",
                (cutoff,),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return []


def _fetch_freezes_24h() -> int:
    cutoff = int(time.time() * 1000) - WINDOW_H * 3600 * 1000
    try:
        con = _connect()
        try:
            r = con.execute(
                "SELECT COUNT(*) AS n FROM spot_contradiction_freeze_ticks"
                " WHERE ts_ms >= ?", (cutoff,),
            ).fetchone()
            return int(r["n"] or 0) if r else 0
        finally:
            con.close()
    except Exception:
        return 0


def generate() -> DailyReport:
    """Build the full 24h report. Idempotent — safe to call on-demand
    in addition to the 24h daemon."""
    _init_schema()
    r = DailyReport()
    now = datetime.now(timezone.utc)
    r.report_date = now.strftime("%Y-%m-%d")

    trades = _fetch_trades_24h()
    pretrade = _fetch_pretrade_24h()
    kl_events = _fetch_kill_ladder_events_24h()
    n_freeze = _fetch_freezes_24h()

    enters = [t for t in trades if t["action"] == "enter"]
    exits = [t for t in trades if t["action"] == "exit"]
    skips = [t for t in trades if t["action"] in ("skip", "reject", "bypass_L8")]

    realized_pnl = sum(float(t["pnl_usd"] or 0) for t in exits)
    realized_fee = sum(float(t["fee_usd"] or 0) for t in exits)
    net_pnl = realized_pnl - realized_fee

    wins = [t for t in exits if float(t["pnl_usd"] or 0) > 0]
    losses = [t for t in exits if float(t["pnl_usd"] or 0) < 0]

    # Section 1 — Executive Status
    try:
        from spot_aggro.governance.engine_state_source import current_engine_state
        eng_state = current_engine_state()
    except Exception:
        eng_state = {"state": "unknown"}
    r.executive_status = {
        "engine_state": eng_state.get("state"),
        "n_exits_24h": len(exits),
        "n_enters_24h": len(enters),
        "net_pnl_24h_usd": round(net_pnl, 4),
        "freeze_events_24h": n_freeze,
        "kill_ladder_events_24h": len(kl_events),
    }

    # Section 2 — Activity
    pass_count = sum(1 for p in pretrade if p["passed"])
    r.activity = {
        "pretrade_authz": len(pretrade),
        "pretrade_passed": pass_count,
        "pretrade_rejected": len(pretrade) - pass_count,
        "entries": len(enters),
        "exits": len(exits),
        "skips_rejects": len(skips),
    }

    # Section 3 — Performance
    wr = len(wins) / len(exits) if exits else 0.0
    avg_win_usd = (sum(float(t["pnl_usd"] or 0) for t in wins) / len(wins)) if wins else 0.0
    avg_loss_usd = (sum(float(t["pnl_usd"] or 0) for t in losses) / len(losses)) if losses else 0.0
    r.performance = {
        "wr_24h": round(wr, 4),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "avg_win_usd": round(avg_win_usd, 4),
        "avg_loss_usd": round(avg_loss_usd, 4),
        "net_pnl_usd": round(net_pnl, 4),
        "realized_fee_usd": round(realized_fee, 4),
    }

    # Section 4 — Critical Gaps (auto-detected from live signals)
    gaps: list[str] = []
    if len(exits) < 5:
        gaps.append(f"sample too small: {len(exits)} exits in 24h (need >=20 for statistical rigor)")
    if len(enters) > 0 and (len(exits) == 0):
        gaps.append(f"{len(enters)} entries, 0 exits — exit path may be stalled")
    if len(kl_events) > 0:
        gaps.append(f"{len(kl_events)} kill-ladder transitions — check alert log")
    if n_freeze > 0:
        gaps.append(f"{n_freeze} contradiction-freeze ticks — investigate")
    r.critical_gaps = gaps

    # Section 5 — Root Cause (from formula_review latest)
    try:
        from spot_aggro.governance.formula_review import latest as fr_latest
        fr = fr_latest(limit=1)
        if fr:
            r.root_cause = fr[0].headline or fr[0].rationale or "no recent formula-review"
            r.upgrade_focus = fr[0].top_upgrade
        else:
            r.root_cause = "no formula review verdict yet"
    except Exception as e:
        r.root_cause = f"formula_review error: {str(e)[:100]}"

    # Section 6 — Governance Decision
    try:
        from spot_aggro.governance.kill_ladder import current_state as kl_state
        ladder = kl_state().level
    except Exception:
        ladder = "?"
    decision = []
    if n_freeze > 0:
        decision.append("freeze active — block new entries")
    if ladder != "L0":
        decision.append(f"kill-ladder at {ladder} — manual review required")
    if len(exits) >= 20 and wr < 0.30:
        decision.append("WR<30% — consider promote variant switch")
    if not decision:
        decision.append("continue observe")
    r.governance_decision = {"actions": decision, "kill_ladder": ladder}

    # Section 9 — Strategy Progress Toward >=2%/trade Target
    try:
        from spot_aggro.governance.strategy_sufficiency import evaluate as suff_eval
        suff = suff_eval()
        if suff.recommendation == "insufficient_sample":
            path = "insufficient_data"
        elif suff.recommendation == "keep":
            path = "realistic"
        elif suff.recommendation == "tune":
            path = "weak"
        else:
            path = "failing"
        r.strategy_progress = {
            "path_to_target": path,
            "target_pct_per_trade": suff.target_pct_per_trade,
            "hit_rate": suff.pct_hitting_target,
            "wilson_low": suff.wilson_low,
            "n_observed": suff.n_observed,
            "required_wr": suff.required_wr_for_target,
            "observed_wr": round(
                suff.n_wins / suff.n_observed, 4
            ) if suff.n_observed else 0.0,
            "recommendation": suff.recommendation,
            "reason": suff.reason,
        }
    except Exception as e:
        r.strategy_progress = {"error": str(e)[:140]}

    # Verdict aggregation.
    prog = r.strategy_progress.get("path_to_target", "unknown")
    if prog == "realistic":
        r.verdict = "improving"
    elif prog in ("weak", "failing"):
        r.verdict = "degrading" if net_pnl < 0 else "flat"
    else:
        r.verdict = "insufficient"

    if len(exits) == 0:
        r.headline = f"{r.report_date}: zero exits; data-collection phase"
    else:
        r.headline = (
            f"{r.report_date}: {len(exits)} exits, WR={wr * 100:.0f}%, "
            f"net=${net_pnl:+.2f}, path={prog}"
        )

    # Render markdown.
    r.markdown = _render_markdown(r)

    # Persist.
    try:
        con = _connect()
        try:
            con.execute(
                "INSERT INTO spot_daily_reports("
                " ts_ms, report_date, verdict, headline, payload_json, markdown"
                ") VALUES(?,?,?,?,?,?)",
                (r.ts_ms, r.report_date, r.verdict, r.headline,
                 json.dumps(r.to_dict(), default=str), r.markdown),
            )
        finally:
            con.close()
    except Exception:
        pass

    # Write markdown to reports/ dir.
    try:
        reports_dir = Path("reports")
        reports_dir.mkdir(exist_ok=True)
        out = reports_dir / f"daily_{r.report_date}.md"
        out.write_text(r.markdown, encoding="utf-8")
    except Exception:
        pass
    return r


def _render_markdown(r: DailyReport) -> str:
    es = r.executive_status
    a = r.activity
    p = r.performance
    gd = r.governance_decision
    sp = r.strategy_progress
    gaps = "\n".join(f"- {g}" for g in r.critical_gaps) or "- none"
    decisions = "\n".join(f"- {x}" for x in gd.get("actions", [])) or "- none"
    return f"""# DAILY GOVERNANCE REPORT — {r.report_date}

**Verdict:** {r.verdict.upper()} | **Headline:** {r.headline}

## 1. Executive Status
engine={es.get('engine_state')} | entries={es.get('n_enters_24h')} | exits={es.get('n_exits_24h')} | net_pnl=${es.get('net_pnl_24h_usd'):+.2f} | freezes={es.get('freeze_events_24h')} | kill_events={es.get('kill_ladder_events_24h')}

## 2. Activity Snapshot
pretrade={a.get('pretrade_authz')} (pass {a.get('pretrade_passed')}/rej {a.get('pretrade_rejected')}) | entries={a.get('entries')} | exits={a.get('exits')} | skips={a.get('skips_rejects')}

## 3. Performance Reality
WR {p.get('wr_24h', 0) * 100:.0f}% ({p.get('n_wins')}/{p.get('n_wins', 0) + p.get('n_losses', 0)}) | avg_win ${p.get('avg_win_usd'):+.4f} | avg_loss ${p.get('avg_loss_usd'):+.4f} | net ${p.get('net_pnl_usd'):+.2f} | fees ${p.get('realized_fee_usd'):.4f}

## 4. Critical Gaps
{gaps}

## 5. Root Cause (1)
{r.root_cause}

## 6. Governance Decision
kill_ladder={gd.get('kill_ladder')}
{decisions}

## 7. Upgrade Focus
{r.upgrade_focus or 'none identified'}

## 8. Daily Verdict
system: **{r.verdict.upper()}**

## 9. Strategy Progress Toward >=2%/trade Target
path: **{sp.get('path_to_target', '?').upper()}** | hit_rate {sp.get('hit_rate', 0) * 100:.0f}% | wilson_low {sp.get('wilson_low', 0) * 100:.0f}% | n={sp.get('n_observed')} | required_wr {sp.get('required_wr', 0) * 100:.0f}% | observed_wr {sp.get('observed_wr', 0) * 100:.0f}% | rec: **{sp.get('recommendation', '?').upper()}**
reason: {sp.get('reason', '')}
"""


def latest(limit: int = 10) -> list[dict[str, Any]]:
    try:
        _init_schema()
        con = _connect()
        try:
            rows = con.execute(
                "SELECT id, ts_ms, report_date, verdict, headline"
                " FROM spot_daily_reports"
                " ORDER BY ts_ms DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        finally:
            con.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def latest_full() -> dict[str, Any] | None:
    try:
        _init_schema()
        con = _connect()
        try:
            r = con.execute(
                "SELECT * FROM spot_daily_reports"
                " ORDER BY ts_ms DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()
        if not r:
            return None
        return {
            "id": r["id"], "ts_ms": r["ts_ms"],
            "report_date": r["report_date"],
            "verdict": r["verdict"],
            "headline": r["headline"],
            "markdown": r["markdown"],
            "payload": json.loads(r["payload_json"] or "{}"),
        }
    except Exception:
        return None
