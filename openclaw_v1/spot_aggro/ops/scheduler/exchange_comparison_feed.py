"""Phase 11n-9-qq — Exchange comparison feed daemon.

Periodically fetches ticker + depth for tracked symbols from BOTH
OKX (existing live adapter) and Crypto.com (read-only public API).
Writes one row per (symbol, exchange, ts_ms) to
`spot_exchange_comparison` so governance + dashboard can audit
per-exchange liquidity and spread quality.

Never places orders. Never reads balances. Crypto.com here is a
third-party market-data source only. Its failure does not affect
OKX trading or any live execution path.

Configurable cadence via env SPOT_EXCHANGE_COMPARE_INTERVAL_S
(default 60). Symbol list is the 20 tier-C admitted cells + BTC/ETH
as anchors.

Fail-open across both exchanges. One missing side still writes the
other side.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

log = logging.getLogger(__name__)

INTERVAL_ENV = "SPOT_EXCHANGE_COMPARE_INTERVAL_S"
DEFAULT_INTERVAL_S = 60

# Anchor symbols — always compared even if not in admitted universe.
ANCHOR_SYMBOLS = ("BTC-USDT", "ETH-USDT")


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
            "CREATE TABLE IF NOT EXISTS spot_exchange_comparison("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts_ms INTEGER NOT NULL,"
            " symbol TEXT NOT NULL,"
            " exchange TEXT NOT NULL,"             # 'okx' | 'cryptocom'
            " last REAL, bid REAL, ask REAL,"
            " spread_bp REAL,"
            " bid_depth_usd REAL, ask_depth_usd REAL, top_depth_usd REAL,"
            " volume_24h REAL,"
            " ok INTEGER NOT NULL DEFAULT 1,"
            " error TEXT,"
            " payload_json TEXT"
            ")"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_excmp_ts "
            "ON spot_exchange_comparison(ts_ms DESC)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_excmp_sym_ex "
            "ON spot_exchange_comparison(symbol, exchange, ts_ms DESC)"
        )
    finally:
        con.close()


@dataclass
class ComparisonRow:
    ts_ms: int
    symbol: str
    exchange: str
    last: float
    bid: float
    ask: float
    spread_bp: float
    bid_depth_usd: float
    ask_depth_usd: float
    top_depth_usd: float
    volume_24h: float | None = None
    ok: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _persist(row: ComparisonRow) -> None:
    try:
        con = _connect()
        try:
            con.execute(
                "INSERT INTO spot_exchange_comparison("
                " ts_ms, symbol, exchange, last, bid, ask, spread_bp,"
                " bid_depth_usd, ask_depth_usd, top_depth_usd,"
                " volume_24h, ok, error, payload_json"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row.ts_ms, row.symbol, row.exchange,
                    row.last, row.bid, row.ask, row.spread_bp,
                    row.bid_depth_usd, row.ask_depth_usd, row.top_depth_usd,
                    row.volume_24h, int(row.ok), row.error,
                    json.dumps(row.to_dict(), default=str),
                ),
            )
        finally:
            con.close()
    except Exception as e:
        log.debug("comparison persist failed: %s", e)


def _tracked_symbols() -> list[str]:
    """Admitted tier-C cells + anchor BTC/ETH."""
    try:
        from spot_aggro.governance.universe_gatekeeper import all_admissions
        cells = all_admissions()
        syms = set()
        for c in cells:
            if getattr(c, "state", "") != "admitted":
                continue
            key = getattr(c, "cell_key", "")
            # cell_key = "C|ENA-USDT"
            if "|" in key:
                syms.add(key.split("|", 1)[1])
        syms.update(ANCHOR_SYMBOLS)
        return sorted(syms)
    except Exception:
        return list(ANCHOR_SYMBOLS)


async def _fetch_okx_row(symbol: str, ts_ms: int) -> ComparisonRow:
    """Fetch ticker + depth from OKX via existing adapter."""
    try:
        from shared.adapters.okx_unified import OKXUnified
        a = OKXUnified(engine="spot_aggro")
        tick = await a.get_spot_ticker(symbol)
        depth_usd = await a.get_spot_book_depth_usd(symbol, levels=5)
        last = float(tick.get("last") or 0)
        # OKX ccxt ticker has bid/ask/spread_bp if our adapter computed it.
        bid = float(tick.get("bid") or tick.get("bidPrice") or 0)
        ask = float(tick.get("ask") or tick.get("askPrice") or 0)
        spread_bp = float(tick.get("spread_bp") or 0)
        if spread_bp == 0 and bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
            spread_bp = (ask - bid) / mid * 10_000.0 if mid else 0.0
        return ComparisonRow(
            ts_ms=ts_ms, symbol=symbol, exchange="okx",
            last=last, bid=bid, ask=ask,
            spread_bp=round(spread_bp, 2),
            bid_depth_usd=round(depth_usd, 2),
            ask_depth_usd=round(depth_usd, 2),
            top_depth_usd=round(depth_usd, 2),
            volume_24h=float(tick.get("quoteVolume") or tick.get("baseVolume") or 0) or None,
            ok=True,
        )
    except Exception as e:
        return ComparisonRow(
            ts_ms=ts_ms, symbol=symbol, exchange="okx",
            last=0, bid=0, ask=0, spread_bp=0,
            bid_depth_usd=0, ask_depth_usd=0, top_depth_usd=0,
            ok=False, error=str(e)[:160],
        )


async def _fetch_cdc_row(symbol: str, ts_ms: int) -> ComparisonRow:
    """Fetch ticker + depth from Crypto.com public API."""
    try:
        from shared.adapters.cryptocom_public import fetch_ticker_and_depth
        t, d = await fetch_ticker_and_depth(symbol, levels=20)
        if t is None and d is None:
            return ComparisonRow(
                ts_ms=ts_ms, symbol=symbol, exchange="cryptocom",
                last=0, bid=0, ask=0, spread_bp=0,
                bid_depth_usd=0, ask_depth_usd=0, top_depth_usd=0,
                ok=False, error="both_ticker_and_depth_missing",
            )
        last = t.last if t else 0.0
        bid = t.bid if t else 0.0
        ask = t.ask if t else 0.0
        spread_bp = t.spread_bp if t else 0.0
        bid_depth = d.bid_depth_usd if d else 0.0
        ask_depth = d.ask_depth_usd if d else 0.0
        top_depth = d.top_depth_usd if d else 0.0
        vol = t.volume_24h if t else None
        partial = (t is None) or (d is None)
        return ComparisonRow(
            ts_ms=ts_ms, symbol=symbol, exchange="cryptocom",
            last=last, bid=bid, ask=ask, spread_bp=spread_bp,
            bid_depth_usd=bid_depth, ask_depth_usd=ask_depth,
            top_depth_usd=top_depth,
            volume_24h=vol,
            ok=not partial,
            error="partial_missing_depth" if (partial and t) else
                  ("partial_missing_ticker" if (partial and d) else None),
        )
    except Exception as e:
        return ComparisonRow(
            ts_ms=ts_ms, symbol=symbol, exchange="cryptocom",
            last=0, bid=0, ask=0, spread_bp=0,
            bid_depth_usd=0, ask_depth_usd=0, top_depth_usd=0,
            ok=False, error=str(e)[:160],
        )


async def fetch_one_cycle(symbols: list[str] | None = None) -> int:
    """Fetch OKX + CDC for every tracked symbol in parallel. Returns
    total rows persisted. Safe to call on-demand."""
    _init_schema()
    syms = symbols or _tracked_symbols()
    if not syms:
        return 0
    ts_ms = int(time.time() * 1000)
    # Build task list: 2 per symbol (okx + cdc).
    tasks = []
    for s in syms:
        tasks.append(_fetch_okx_row(s, ts_ms))
        tasks.append(_fetch_cdc_row(s, ts_ms))
    rows = await asyncio.gather(*tasks, return_exceptions=False)
    persisted = 0
    for row in rows:
        if row is not None:
            _persist(row)
            persisted += 1
    return persisted


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

_thread: threading.Thread | None = None
_stop = threading.Event()


def start() -> None:
    """Idempotent daemon start. Called by server.py startup."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    try:
        interval = int(os.environ.get(INTERVAL_ENV, str(DEFAULT_INTERVAL_S)))
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_S

    def _loop() -> None:
        log.info(
            "spot_aggro exchange_comparison_feed started (interval=%ds)",
            interval,
        )
        # First cycle immediately so dashboard has data on boot.
        try:
            n = asyncio.run(fetch_one_cycle())
            log.info(
                "[exchange_comparison] boot cycle persisted %d rows", n,
            )
        except Exception as e:
            log.warning("exchange_comparison boot cycle failed: %s", e)
        while not _stop.is_set():
            if _stop.wait(timeout=interval):
                break
            try:
                n = asyncio.run(fetch_one_cycle())
                if n > 0:
                    log.debug("[exchange_comparison] persisted %d rows", n)
            except Exception as e:
                log.warning("exchange_comparison cycle failed: %s", e)
        log.info("spot_aggro exchange_comparison_feed stopped")

    _thread = threading.Thread(
        target=_loop, name="spot-exchange-comparison", daemon=True,
    )
    _thread.start()


def stop() -> None:
    _stop.set()


# ---------------------------------------------------------------------------
# Read helpers (used by /gov/exchange_comparison endpoint + governance board)
# ---------------------------------------------------------------------------

def latest_comparison_rows(window_min: int = 30) -> list[dict[str, Any]]:
    """Return the freshest (symbol, exchange) row pair within window."""
    _init_schema()
    try:
        con = _connect()
        try:
            now = int(time.time() * 1000)
            cutoff = now - window_min * 60_000
            # Grab freshest row per (symbol, exchange).
            rows = con.execute(
                "SELECT symbol, exchange, MAX(ts_ms) AS ts_ms, last, bid, ask,"
                " spread_bp, bid_depth_usd, ask_depth_usd, top_depth_usd,"
                " volume_24h, ok, error"
                " FROM spot_exchange_comparison"
                " WHERE ts_ms >= ?"
                " GROUP BY symbol, exchange"
                " ORDER BY symbol, exchange",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()
    except Exception:
        return []


def per_symbol_gap(window_min: int = 30) -> list[dict[str, Any]]:
    """For each tracked symbol, compute OKX-vs-CDC deltas.

    Returns: [{symbol, okx_depth, cdc_depth, depth_winner,
               okx_spread_bp, cdc_spread_bp, spread_winner,
               price_drift_bp, both_ok}]
    """
    rows = latest_comparison_rows(window_min=window_min)
    by_symbol: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        s = r["symbol"]
        ex = r["exchange"]
        by_symbol.setdefault(s, {})[ex] = r

    out: list[dict[str, Any]] = []
    for s, exs in sorted(by_symbol.items()):
        okx = exs.get("okx")
        cdc = exs.get("cryptocom")
        okx_depth = float((okx or {}).get("top_depth_usd") or 0)
        cdc_depth = float((cdc or {}).get("top_depth_usd") or 0)
        okx_spread = float((okx or {}).get("spread_bp") or 0)
        cdc_spread = float((cdc or {}).get("spread_bp") or 0)
        okx_last = float((okx or {}).get("last") or 0)
        cdc_last = float((cdc or {}).get("last") or 0)
        price_drift_bp = 0.0
        if okx_last > 0 and cdc_last > 0:
            mid = (okx_last + cdc_last) / 2.0
            price_drift_bp = abs(okx_last - cdc_last) / mid * 10_000.0
        depth_winner = (
            "okx" if okx_depth > cdc_depth else
            "cryptocom" if cdc_depth > okx_depth else "tie"
        )
        spread_winner = (
            "okx" if okx_spread < cdc_spread and okx_spread > 0 else
            "cryptocom" if cdc_spread < okx_spread and cdc_spread > 0 else "tie"
        )
        out.append({
            "symbol": s,
            "okx": okx, "cryptocom": cdc,
            "okx_depth_usd": round(okx_depth, 2),
            "cdc_depth_usd": round(cdc_depth, 2),
            "depth_winner": depth_winner,
            "okx_spread_bp": okx_spread,
            "cdc_spread_bp": cdc_spread,
            "spread_winner": spread_winner,
            "price_drift_bp": round(price_drift_bp, 2),
            "both_ok": bool(okx and cdc and okx.get("ok") and cdc.get("ok")),
        })
    return out
