"""
Forensic Accuracy Review Runner — permanent spot_aggro subsystem.

Runs every 6 hours (configurable). Generates PDF reports.
5 LLMs analyze all trades in the review period.
Reports stored permanently in forensic/reports/.

SPOT_AGGRO ONLY. Zero apex_omega.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from typing import Any, Optional

from shared.persistence import state as persist
from .models import ForensicReport, TradeReview
from . import prompts as forensic_prompts
from .pdf_renderer import render_pdf

log = logging.getLogger("spot_aggro.forensic")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_reports: list[ForensicReport] = []
_lock = threading.Lock()

REVIEW_INTERVAL_S = 21600  # 6 hours
LOOKBACK_S = 21600         # review last 6 hours of trades


def get_reports() -> list[dict]:
    with _lock:
        return [r.to_dict() for r in _reports]


def get_report(report_id: str) -> Optional[ForensicReport]:
    with _lock:
        for r in _reports:
            if r.report_id == report_id:
                return r
    return None


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def _collect_trades(period_start: float, period_end: float) -> list[dict]:
    """Get all trades in the period from the trade log."""
    try:
        con = persist._connect()
        rows = con.execute(
            "SELECT * FROM apex_trade_log WHERE ts_ms >= ? AND ts_ms <= ? ORDER BY ts_ms ASC",
            (int(period_start * 1000), int(period_end * 1000)),
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.warning("forensic trade collection failed: %s", exc)
        return []


def _format_trade_log(trades: list[dict]) -> str:
    lines = []
    for t in trades[:50]:
        sym = t.get("symbol", "?")
        act = t.get("action", "?")
        mod = t.get("module", "?")
        pnl = t.get("pnl_usd")
        notional = t.get("notional_usd", 0)
        payload = t.get("payload_json", "")
        tier = ""
        m = re.search(r'"tier":\s*"([^"]+)"', payload or "")
        if m:
            tier = m.group(1)
        reason = ""
        m2 = re.search(r'"reason":\s*"([^"]+)"', payload or "")
        if m2:
            reason = m2.group(1)
        pnl_str = f"pnl=${pnl:+.4f}" if pnl else ""
        lines.append(f"  {act:8} {sym:12} [{tier:3}] ${notional or 0:.2f} {pnl_str} {reason}")
    return "\n".join(lines) or "  (no trades)"


def _build_trade_reviews(trades: list[dict]) -> list[TradeReview]:
    """Build TradeReview objects from raw trade log."""
    reviews = []
    # Pair enters with exits
    enters = {}
    for t in trades:
        sym = t.get("symbol", "?")
        act = t.get("action", "?")
        payload = json.loads(t.get("payload_json", "{}") or "{}")

        if act == "enter":
            enters[sym] = t
        elif act == "exit" and sym in enters:
            entry = enters.pop(sym)
            pnl = float(t.get("pnl_usd") or 0)
            notional = float(entry.get("notional_usd") or 0)
            ret = pnl / notional if notional > 0 else 0

            entry_payload = json.loads(entry.get("payload_json", "{}") or "{}")
            reviews.append(TradeReview(
                symbol=sym,
                action="round_trip",
                tier=entry_payload.get("tier", "?"),
                side="buy",
                notional_usd=notional,
                entry_ts=float(entry.get("ts_ms", 0)) / 1000,
                exit_ts=float(t.get("ts_ms", 0)) / 1000,
                pnl_usd=pnl,
                ret_pct=ret,
                verdict="GREEN" if pnl > 0.001 else "RED" if pnl < -0.001 else "FLAT",
                entry_composite=float(entry_payload.get("composite", 0)),
                exit_reason=payload.get("reason", "?"),
                quality="",
                root_cause="",
                fix="",
                was_false_positive=False,
                was_false_negative=False,
            ))
    return reviews


# ---------------------------------------------------------------------------
# Parse helper
# ---------------------------------------------------------------------------

def _parse_json(raw: str) -> Optional[dict]:
    try:
        clean = raw.strip()
        clean = re.sub(r"^```json\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean)
        m = re.search(r"\{.*\}", clean, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# 5-LLM forensic analysis
# ---------------------------------------------------------------------------

async def run_forensic_review(engine_ref: Any) -> ForensicReport:
    """Run full 5-LLM forensic review. Returns ForensicReport."""
    from shared.llm.consensus import _call_member

    now = time.time()
    period_end = now
    period_start = now - LOOKBACK_S

    # Collect data
    trades = _collect_trades(period_start, period_end)
    reviews = _build_trade_reviews(trades)

    # Stats
    greens = [r for r in reviews if r.verdict == "GREEN"]
    reds = [r for r in reviews if r.verdict == "RED"]
    flats = [r for r in reviews if r.verdict == "FLAT"]
    total_pnl = sum(r.pnl_usd for r in reviews)
    win_rate = len(greens) / max(len(reviews), 1)
    avg_win = sum(r.pnl_usd for r in greens) / max(len(greens), 1)
    avg_loss = sum(r.pnl_usd for r in reds) / max(len(reds), 1)

    report_id = hashlib.md5(f"{now}".encode()).hexdigest()[:12]

    report = ForensicReport(
        report_id=report_id,
        generated_at=now,
        period_start=period_start,
        period_end=period_end,
        total_trades=len(reviews),
        greens=len(greens),
        reds=len(reds),
        flats=len(flats),
        total_pnl=total_pnl,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        expectancy=avg_win * win_rate + avg_loss * (1 - win_rate) if reviews else 0,
        trade_reviews=reviews,
    )

    # Build context
    trade_log = _format_trade_log(trades)

    # Get universe state
    rankings = getattr(engine_ref, '_rank_cache', []) or []
    composite_str = "\n".join(
        f"  {r.get('symbol','?'):12} comp={r.get('composite',0):.3f} spi={r.get('spi',0):.3f}"
        for r in rankings[:10]
    )

    ctx = forensic_prompts.TRADE_CONTEXT.format(
        period_start=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(period_start)),
        period_end=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(period_end)),
        total_trades=len(reviews), greens=len(greens), reds=len(reds), flats=len(flats),
        total_pnl=total_pnl, win_rate=win_rate,
        avg_win=avg_win, avg_loss=avg_loss,
        trade_log=trade_log,
        universe_state=f"{len(rankings)} coins ranked",
        composite_scores=composite_str or "  (no scores)",
    )

    total_cost = 0.0
    agents_ok = 0

    # Agent configs
    agents = [
        ("structure", "anthropic", "claude-haiku-4-5", forensic_prompts.STRUCTURE_FORENSIC),
        ("quant", "openai", "gpt-4o-mini", forensic_prompts.QUANT_FORENSIC),
        ("liquidity", "gemini", "gemini-2.5-flash", forensic_prompts.LIQUIDITY_FORENSIC),
        ("regime", "openrouter", "deepseek/deepseek-chat-v3", forensic_prompts.REGIME_FORENSIC),
    ]

    # Phase 1: 4 analysts in parallel
    async def _call(role, provider, model, prompt_tmpl):
        prompt = prompt_tmpl.replace("{context}", ctx)
        try:
            text, cost = await asyncio.wait_for(
                _call_member(role=f"forensic_{role}", provider=provider, model=model, prompt=prompt),
                timeout=30,
            )
            return role, _parse_json(text), cost
        except Exception as exc:
            log.debug("forensic %s failed: %s", role, exc)
            return role, None, 0.0

    results = await asyncio.gather(*[_call(*a) for a in agents])

    analysis = {}
    for role, parsed, cost in results:
        total_cost += cost
        if parsed:
            agents_ok += 1
            analysis[role] = parsed
        else:
            analysis[role] = {}

    # Collect findings
    struct = analysis.get("structure", {})
    quant = analysis.get("quant", {})
    liq = analysis.get("liquidity", {})
    regime = analysis.get("regime", {})

    report.poor_entries = int(struct.get("poor_entries", 0))
    report.poor_exits = int(struct.get("poor_exits", 0))
    report.false_positives = int(quant.get("false_positives", 0))
    report.ranking_mistakes = int(quant.get("ranking_mistakes", 0))
    report.threshold_mistakes = int(quant.get("threshold_mistakes", 0))
    report.false_negatives = int(regime.get("false_negatives", 0))
    report.structure_analysis = struct.get("analysis", "")
    report.quant_analysis = quant.get("analysis", "")
    report.liquidity_analysis = liq.get("analysis", "")
    report.regime_analysis = regime.get("analysis", "")

    # Phase 2: Adjudicator
    adj_prompt = forensic_prompts.ADJUDICATOR_FORENSIC.format(
        structure_summary=report.structure_analysis[:200],
        quant_summary=report.quant_analysis[:200],
        liquidity_summary=report.liquidity_analysis[:200],
        regime_summary=report.regime_analysis[:200],
        total_trades=len(reviews), greens=len(greens), reds=len(reds),
        total_pnl=total_pnl, win_rate=win_rate,
    )
    try:
        adj_text, adj_cost = await asyncio.wait_for(
            _call_member(role="forensic_adjudicator", provider="mistral",
                         model="mistral-small-latest", prompt=adj_prompt),
            timeout=30,
        )
        total_cost += adj_cost
        adj = _parse_json(adj_text)
        if adj:
            agents_ok += 1
            report.executive_summary = adj.get("executive_summary", "")
            report.top_issues = adj.get("top_issues", [])
            report.recommended_fixes = adj.get("recommended_fixes", [])
    except Exception:
        pass

    report.cost_usd = total_cost
    report.agents_ok = agents_ok

    # Render PDF
    try:
        pdf_path = render_pdf(report)
        report.pdf_path = pdf_path
        log.info("forensic report %s: %d trades, PnL=$%.4f, PDF=%s",
                 report_id, len(reviews), total_pnl, pdf_path)
    except Exception as exc:
        log.warning("PDF render failed: %s", exc)

    # Store
    with _lock:
        _reports.append(report)
        # Keep last 30 reports
        if len(_reports) > 30:
            _reports[:] = _reports[-30:]

    return report


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def start(engine_ref: Any) -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()

    def _loop():
        log.info("forensic reviewer started (interval=%ds)", REVIEW_INTERVAL_S)
        time.sleep(300)  # 5 min warmup — let engine accumulate trades

        while not _stop.is_set():
            try:
                asyncio.run(run_forensic_review(engine_ref))
            except Exception:
                log.exception("forensic review crashed (recovering)")
            for _ in range(REVIEW_INTERVAL_S):
                if _stop.is_set():
                    return
                time.sleep(1)

    _thread = threading.Thread(target=_loop, name="spot_aggro_forensic", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
