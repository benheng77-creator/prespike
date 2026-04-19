"""
LLM-driven error watchdog. Every time the engine logs an exception or a
trade is rejected with an error, a compact failure snapshot is queued.
The watchdog asks an LLM to classify the root cause and suggest the
minimal remediation (config change / operator action / code fix).

Output goes to watchdog_findings for operator review. This module
DOES NOT auto-apply code changes — that remains a human decision — but
it produces concrete, actionable lines like:
    "ROOT: OKX 51008 = 'Insufficient margin'. Lower max_notional_pct
     to 0.005 OR top up USDT on the unified account."
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from ..persistence import state as persist


log = logging.getLogger("apex.watchdog")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchdog_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    source          TEXT NOT NULL,            -- engine | module | adapter | llm | reconciler
    symbol          TEXT,
    error_class     TEXT,
    error_msg       TEXT,
    context_json    TEXT,
    processed       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_wd_q_ts ON watchdog_queue(processed, ts_ms);

CREATE TABLE IF NOT EXISTS watchdog_findings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id        INTEGER NOT NULL,
    ts_ms           INTEGER NOT NULL,
    root_cause      TEXT,
    remediation     TEXT,
    severity        TEXT,           -- info | warn | critical
    suggested_config_patch_json TEXT,
    llm_provider    TEXT,
    llm_model       TEXT,
    llm_latency_ms  INTEGER,
    FOREIGN KEY(queue_id) REFERENCES watchdog_queue(id)
);
CREATE INDEX IF NOT EXISTS idx_wd_f_ts ON watchdog_findings(ts_ms DESC);
"""


_lock = threading.Lock()
_initialized = False
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def _init() -> None:
    global _initialized
    with _lock:
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


def enqueue(*, source: str, error_class: str, error_msg: str,
            symbol: Optional[str] = None,
            context: Optional[dict[str, Any]] = None) -> None:
    """Add an error to the watchdog queue. Safe to call from anywhere."""
    _init()
    try:
        con = persist._connect()
        try:
            con.execute(
                "INSERT INTO watchdog_queue (ts_ms, source, symbol, error_class, error_msg, context_json) VALUES (?, ?, ?, ?, ?, ?)",
                (int(time.time()*1000), source, symbol, error_class,
                 error_msg[:400],
                 json.dumps(context or {}, default=str)),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        log.exception("watchdog enqueue failed")


def list_unprocessed(
    limit: int = 5,
    engine: str | None = None,
) -> list[dict[str, Any]]:
    _init()
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT * FROM watchdog_queue WHERE processed = 0 "
            "ORDER BY ts_ms ASC LIMIT ?", (limit,),
        ).fetchall()
    finally:
        con.close()
    records = [dict(r) for r in rows]
    if engine == "spot":
        records = [
            r for r in records
            if not (r.get("source") or "").startswith(_PERP_SOURCE_PREFIXES)
        ]
    return records


def mark_processed(queue_id: int) -> None:
    con = persist._connect()
    try:
        con.execute("UPDATE watchdog_queue SET processed=1 WHERE id=?", (queue_id,))
        con.commit()
    finally:
        con.close()


_PERP_SOURCE_PREFIXES = (
    "funding_hunt",
    "stat_arb",
    "triangular",
    "liq_fade",
)


def recent_findings(
    limit: int = 20,
    engine: str | None = None,
) -> list[dict[str, Any]]:
    """Return watchdog findings.

    When `engine='spot'`, rows whose source originated in perp-only modules
    (funding_hunt, stat_arb, triangular, liq_fade) are filtered out so the
    SPOT dashboard never surfaces perp noise. Other engines pass through.
    """
    _init()
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT w.*, q.source, q.symbol, q.error_class, q.error_msg "
            "FROM watchdog_findings w LEFT JOIN watchdog_queue q "
            "ON w.queue_id = q.id ORDER BY w.ts_ms DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        con.close()

    records = [dict(r) for r in rows]
    if engine == "spot":
        records = [
            r for r in records
            if not (r.get("source") or "").startswith(_PERP_SOURCE_PREFIXES)
        ]
    return records


# ---------------------------------------------------------------------------
# LLM root-cause analyser
# ---------------------------------------------------------------------------

_PROMPT = (
    "You are an APEX-Omega trading-system root-cause analyser. Classify this "
    "error and return STRICT JSON.\n\n"
    "source: {source}\n"
    "error_class: {error_class}\n"
    "error_msg: {error_msg}\n"
    "symbol: {symbol}\n"
    "context: {context}\n\n"
    "Respond ONLY with compact JSON:\n"
    '{{"root_cause": "<one sentence>", '
    '"severity": "info|warn|critical", '
    '"remediation": "<specific actionable steps, no fluff>", '
    '"suggested_config_patch": <null or object with keys to change>}}'
)


async def analyse_one(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Ask the cheapest available LLM to classify this error. Never raises."""
    from ..llm.consensus import _call_member          # reuse the provider router
    prompt = _PROMPT.format(
        source=item.get("source"), error_class=item.get("error_class"),
        error_msg=(item.get("error_msg") or "")[:400],
        symbol=item.get("symbol") or "-",
        context=(item.get("context_json") or "{}")[:600],
    )
    providers_tried = []
    for provider, model in (
        ("gemini", "gemini-2.5-flash"),
        ("mistral", "mistral-small-latest"),
        ("openai", "gpt-4o-mini"),
    ):
        providers_tried.append(f"{provider}:{model}")
        try:
            started = time.time()
            text, cost = await asyncio.wait_for(
                _call_member(role="watchdog", provider=provider,
                             model=model, prompt=prompt),
                timeout=12,
            )
            latency_ms = int((time.time() - started) * 1000)
            try:
                obj = json.loads(text.strip().strip("`"))
            except Exception:
                # try to extract {...}
                import re
                m = re.search(r"\{.*\}", text, re.DOTALL)
                if not m:
                    continue
                obj = json.loads(m.group(0))
            return {
                "queue_id": item["id"],
                "root_cause": str(obj.get("root_cause", ""))[:400],
                "remediation": str(obj.get("remediation", ""))[:800],
                "severity": str(obj.get("severity", "info")),
                "suggested_config_patch":
                    obj.get("suggested_config_patch") or None,
                "llm_provider": provider,
                "llm_model": model,
                "llm_latency_ms": latency_ms,
            }
        except Exception as exc:
            log.debug("watchdog analyse via %s/%s failed: %s", provider, model, exc)
            continue
    log.warning("watchdog could not analyse item id=%s (tried %s)",
                item["id"], providers_tried)
    return None


async def process_once(limit: int = 3) -> int:
    _init()
    items = list_unprocessed(limit=limit)
    if not items:
        return 0
    n_ok = 0
    for item in items:
        finding = await analyse_one(item)
        if finding is None:
            mark_processed(item["id"])   # don't retry forever
            continue
        _persist_finding(finding)
        mark_processed(item["id"])
        n_ok += 1
    return n_ok


def _persist_finding(f: dict[str, Any]) -> None:
    con = persist._connect()
    try:
        con.execute(
            "INSERT INTO watchdog_findings (queue_id, ts_ms, root_cause, remediation, severity, suggested_config_patch_json, llm_provider, llm_model, llm_latency_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f["queue_id"], int(time.time()*1000),
             f["root_cause"], f["remediation"], f["severity"],
             json.dumps(f.get("suggested_config_patch"), default=str),
             f["llm_provider"], f["llm_model"], f["llm_latency_ms"]),
        )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Scheduler (optional — runs every 60s)
# ---------------------------------------------------------------------------

def start(interval_s: int = 60) -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()

    def _loop():
        log.info("watchdog loop started (interval=%ds)", interval_s)
        while not _stop.is_set():
            try:
                n = asyncio.run(process_once(limit=3))
                if n:
                    log.info("watchdog processed %d error(s)", n)
            except Exception:
                log.exception("watchdog cycle crashed")
            for _ in range(interval_s):
                if _stop.is_set():
                    return
                time.sleep(1)

    _thread = threading.Thread(target=_loop, name="ops_watchdog", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
