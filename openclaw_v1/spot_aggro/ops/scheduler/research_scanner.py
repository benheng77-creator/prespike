"""
5-minute LLM research scanner.

Every 5 minutes:
    1. Fetch all universe coins with fresh funding rates (bulk endpoint).
    2. Rank by |z-score| × liquidity × spread.
    3. Take top-10 candidates.
    4. Run a SINGLE multi-coin LLM analysis call: "which 3 of these 10
       give the best delta-neutral funding harvest opportunity RIGHT NOW?"
    5. Persist the finding as a research_report row.
    6. Dashboard polls /apex/research/latest to show live findings.

This is SEPARATE from the engine's per-coin consensus call. The engine
still runs its 5-LLM consensus per entry attempt. This scanner gives
the operator a periodic "what is the LLM thinking?" window.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .. import universe_discovery
from ..persistence import state as persist


log = logging.getLogger("apex.research")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS apex_research_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    candidates_n    INTEGER NOT NULL,
    top3_json       TEXT NOT NULL,
    reasoning       TEXT NOT NULL,
    llm_provider    TEXT,
    llm_model       TEXT,
    latency_ms      INTEGER,
    raw_response    TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_ts ON apex_research_reports(ts_ms DESC);
"""

_initialized = False
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_last_report: Optional[dict[str, Any]] = None
_last_lock = threading.Lock()


def _init() -> None:
    global _initialized
    if _initialized:
        return
    persist.init_schema()
    con = persist._connect()
    try:
        con.executescript(_SCHEMA)
        con.commit()
    finally:
        con.close()
    _initialized = True


# ---------------------------------------------------------------------------
# The research prompt — one call covers all candidates
# ---------------------------------------------------------------------------

_PROMPT = """You are the APEX-Omega research analyst. We run a DELTA-NEUTRAL funding
harvest strategy — we hold spot + opposite perp so coin direction is irrelevant.
We profit ONLY from funding rate accrual minus execution cost.

Here are the top {n} coins by funding-rate z-score on OKX right now:

{candidates}

TASK: Pick exactly 3 coins that offer the BEST delta-neutral funding harvest
opportunity for the next 8-24h. Consider:
  * Is the funding rate persistent or a one-off spike?
  * Is liquidity (volume, spread) sufficient for maker execution?
  * Is there event risk (unlock, listing, FOMC) that could blow out the basis?

Respond ONLY with strict JSON:
{{"top3": [{{"symbol": "<SYMBOL>", "reason": "<1 sentence>", "confidence": <0..1>}}, ...],
  "market_summary": "<2 sentences on current funding landscape>",
  "risk_flag": "<any systematic risk across the universe, or 'none'>"}}"""


def _build_candidates_block(coins: list[universe_discovery.DiscoveredCoin]) -> str:
    lines = []
    for i, c in enumerate(coins[:10], 1):
        lines.append(
            f"  {i}. {c.symbol:16} category={c.category:10} "
            f"funding={c.funding_rate*100:+.4f}%  "
            f"vol24h=${c.volume_24h_usd:,.0f}  spread={c.spread_bp:.1f}bp"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Run one research cycle
# ---------------------------------------------------------------------------

async def run_once() -> dict[str, Any]:
    _init()
    coins = universe_discovery.cached_universe()
    if not coins:
        return {"ok": False, "reason": "no universe cache"}

    top10 = coins[:10]
    block = _build_candidates_block(top10)
    prompt = _PROMPT.format(n=len(top10), candidates=block)

    started = time.time()
    try:
        from ..llm.consensus import _call_member
        text, cost = await asyncio.wait_for(
            _call_member(role="research", provider="gemini",
                         model="gemini-2.5-flash", prompt=prompt),
            timeout=20,
        )
    except Exception as exc:
        log.warning("research LLM call failed: %s", exc)
        return {"ok": False, "reason": str(exc)[:200]}

    latency_ms = int((time.time() - started) * 1000)

    # Parse
    import re
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        obj = json.loads(m.group(0)) if m else {}
    except Exception:
        obj = {}

    top3 = obj.get("top3", [])
    summary = obj.get("market_summary", "")
    risk = obj.get("risk_flag", "")

    report = {
        "ts": int(time.time()),
        "candidates_n": len(top10),
        "top3": top3,
        "market_summary": summary,
        "risk_flag": risk,
        "llm_provider": "gemini",
        "llm_model": "gemini-2.5-flash",
        "latency_ms": latency_ms,
    }

    # Persist
    con = persist._connect()
    try:
        con.execute(
            "INSERT INTO apex_research_reports (ts_ms, candidates_n, top3_json, reasoning, llm_provider, llm_model, latency_ms, raw_response) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (int(time.time() * 1000), len(top10),
             json.dumps(top3, default=str),
             summary + (" | risk: " + risk if risk else ""),
             "gemini", "gemini-2.5-flash", latency_ms,
             text[:2000]),
        )
        con.commit()
    finally:
        con.close()

    with _last_lock:
        global _last_report
        _last_report = report

    log.info("research scan done: %d candidates → top3=%s latency=%dms",
             len(top10), [t.get("symbol") for t in top3[:3]], latency_ms)
    return report


def get_latest() -> dict[str, Any]:
    """Read the latest report (from cache or DB)."""
    with _last_lock:
        if _last_report:
            return dict(_last_report)
    _init()
    con = persist._connect()
    try:
        r = con.execute(
            "SELECT ts_ms, candidates_n, top3_json, reasoning, llm_provider, llm_model, latency_ms "
            "FROM apex_research_reports ORDER BY ts_ms DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    if not r:
        return {"ok": False, "reason": "no research reports yet"}
    return {
        "ts": int(r["ts_ms"] / 1000),
        "candidates_n": r["candidates_n"],
        "top3": json.loads(r["top3_json"] or "[]"),
        "market_summary": r["reasoning"],
        "llm_provider": r["llm_provider"],
        "llm_model": r["llm_model"],
        "latency_ms": r["latency_ms"],
    }


def get_history(limit: int = 20) -> list[dict[str, Any]]:
    _init()
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT ts_ms, candidates_n, top3_json, reasoning, latency_ms "
            "FROM apex_research_reports ORDER BY ts_ms DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        con.close()
    return [
        {"ts": int(r["ts_ms"] / 1000),
         "candidates_n": r["candidates_n"],
         "top3": json.loads(r["top3_json"] or "[]"),
         "market_summary": r["reasoning"],
         "latency_ms": r["latency_ms"]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Scheduler — every 5 minutes
# ---------------------------------------------------------------------------

def start(interval_s: int = 300) -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()

    def _loop():
        log.info("research scanner started (interval=%ds)", interval_s)
        # First scan after 30s so universe cache is warm
        for _ in range(30):
            if _stop.is_set(): return
            time.sleep(1)
        while not _stop.is_set():
            try:
                asyncio.run(run_once())
            except Exception:
                log.exception("research scan crashed (recovering)")
            for _ in range(interval_s):
                if _stop.is_set(): return
                time.sleep(1)

    _thread = threading.Thread(target=_loop, name="apex_research_scanner", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
