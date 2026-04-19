"""
End-of-day PnL + activity report.

Pulls data from existing sources (TradeLogger via PersistenceAdapter,
PaperPortfolio via PortfolioAdapter, RiskEngine via RiskAdapter) plus
new sources (AuditLedger for OpenClaw actions) and produces a single
report bundle:

    {
        "date": "YYYY-MM-DD",
        "generated_at_ms": ...,
        "session_window_utc": "00:00..23:59",
        "realized_pnl_quote": <float>,
        "unrealized_pnl_quote": <float>,
        "open_positions": [...],
        "closed_trades": [...],
        "strategy_contribution": {...},
        "variance_notes": [...],
        "actions_summary": {...},
        "scope": "baseline" | "daytrade" | "all"
    }
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def generate_eod(
    *,
    portfolio_adapter: Any,
    persistence_adapter: Optional[Any] = None,
    risk_adapter: Optional[Any] = None,
    ledger: Optional[Any] = None,
    date: Optional[str] = None,
    scope: str = "all",
    out_dir: str = "reports/out",
) -> dict:
    date = date or _today_utc()

    pf = portfolio_adapter.snapshot()
    closed = portfolio_adapter.recent_closed(limit=10_000)
    realized = sum(float(t.get("pnl") or t.get("pnl_quote") or 0) for t in closed)
    unreal = _unrealized(pf)

    persistence_counts = {"decisions": 0, "trades": 0}
    recent_trades = []
    if persistence_adapter is not None:
        persistence_counts = persistence_adapter.counts()
        recent_trades = persistence_adapter.recent_trades(limit=200)

    actions_summary = _summarize_actions(ledger) if ledger is not None else {}

    strategy_contribution = _split_by_session(recent_trades)

    variance_notes: list[str] = []
    if pf.get("balance") is not None and pf.get("starting_balance") is not None:
        delta = float(pf["balance"]) - float(pf["starting_balance"])
        if abs(delta - realized) > 1e-3:
            variance_notes.append(
                f"balance_delta({delta:.4f}) != Σpnl({realized:.4f})"
            )

    report = {
        "date": date,
        "generated_at_ms": int(time.time() * 1000),
        "session_window_utc": f"{date} 00:00..{date} 23:59",
        "scope": scope,
        "realized_pnl_quote": round(realized, 6),
        "unrealized_pnl_quote": round(unreal, 6),
        "open_positions": [pf["position"]] if pf.get("has_open_position") else [],
        "closed_trades_count": pf.get("closed_trades_count", 0),
        "closed_trades_sample": closed[-20:],
        "persistence_counts": persistence_counts,
        "strategy_contribution": strategy_contribution,
        "actions_summary": actions_summary,
        "risk_snapshot": risk_adapter.snapshot() if risk_adapter is not None else None,
        "variance_notes": variance_notes,
    }

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{date}.json"
    md_path = out / f"{date}.md"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md_path.write_text(_markdown(report), encoding="utf-8")
    report["json_path"] = str(json_path)
    report["md_path"] = str(md_path)
    return report


def _unrealized(pf_snapshot: dict) -> float:
    if not pf_snapshot.get("has_open_position"):
        return 0.0
    pos = pf_snapshot.get("position") or {}
    # Without a live mark we can only estimate from stop/target midpoint — report 0 by default.
    return 0.0


def _summarize_actions(ledger: Any) -> dict:
    rows = ledger.fetch_recent(limit=5000)
    by_kind: dict[str, int] = {}
    by_verb: dict[str, int] = {}
    intents = 0
    filled = 0
    failed = 0
    denied = 0
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        if r.get("verb"):
            by_verb[r["verb"]] = by_verb.get(r["verb"], 0) + 1
        if r["kind"] == "intent":
            intents += 1
        elif r["kind"] == "filled":
            filled += 1
        elif r["kind"] == "failed":
            failed += 1
        elif r["kind"] == "finalized" and r.get("result_json") and "denied" in (r["result_json"] or ""):
            denied += 1
    return {
        "total_rows": len(rows),
        "by_kind": by_kind,
        "by_verb": by_verb,
        "intents": intents,
        "filled": filled,
        "failed": failed,
        "denied": denied,
    }


def _split_by_session(trades: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for t in trades:
        sess = t.get("session") or "baseline"
        d = out.setdefault(sess, {"trades": 0, "pnl_quote": 0.0})
        d["trades"] += 1
        d["pnl_quote"] += float(t.get("pnl_quote") or t.get("pnl") or 0)
    return out


def _markdown(r: dict) -> str:
    lines = [
        f"# EOD Report — {r['date']}",
        "",
        f"- Scope: **{r['scope']}**",
        f"- Generated: {r['generated_at_ms']}",
        f"- Realized PnL: **{r['realized_pnl_quote']}**",
        f"- Unrealized PnL: {r['unrealized_pnl_quote']}",
        f"- Closed trades: {r['closed_trades_count']}",
        "",
        "## Actions",
        f"- intents: {r['actions_summary'].get('intents', 0)}",
        f"- filled: {r['actions_summary'].get('filled', 0)}",
        f"- failed: {r['actions_summary'].get('failed', 0)}",
        f"- denied: {r['actions_summary'].get('denied', 0)}",
        "",
        "## Strategy contribution",
    ]
    for k, v in (r.get("strategy_contribution") or {}).items():
        lines.append(f"- {k}: trades={v.get('trades', 0)} pnl={v.get('pnl_quote', 0)}")
    if r.get("variance_notes"):
        lines += ["", "## Variance", *(f"- {n}" for n in r["variance_notes"])]
    return "\n".join(lines) + "\n"
