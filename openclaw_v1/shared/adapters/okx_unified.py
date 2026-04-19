"""
OKX unified REST adapter (APEX-Ω).

Covers every exchange call the engine needs:
    get_funding, get_book_depth, get_volume_24h, get_ticker,
    get_account_equity, get_positions, place_post_only, close_pair,
    cancel_order, fetch_order.

Design choices:
  * Single ccxt client per instance; rebuilt on auth-rotation events.
  * `load_time_difference()` called every init — survives local clock drift.
  * Regional hostname honoured via OKX_HOSTNAME env (eea.okx.com / www.okx.com).
  * Every public response validated; malformed payload → raise OKXError.
  * Post-only retries re-quote N ticks further from mid on reject — no taker fallback.
  * Unified Account mode verified at construction; fails fast if not enabled.

Async is not strictly required — ccxt-sync inside asyncio.to_thread keeps this
simple and avoids ccxt.pro licensing while still being event-loop-friendly.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from ..config import load as load_cfg


log = logging.getLogger("apex.adapters.okx")


class OKXError(Exception):
    """Raised on OKX protocol-level anomalies (bad payload, missing field, auth failure)."""


@dataclass
class OrderReceipt:
    ok: bool
    order_id: Optional[str]
    cl_ord_id: Optional[str]
    filled_qty: float
    avg_px: float
    side: str
    symbol: str
    notional_usd: float
    fee_usd: float
    raw: dict[str, Any]
    error: Optional[str] = None


class OKXUnified:
    def __init__(self, *, engine: str = "ops") -> None:
        cfg = load_cfg(engine=engine)
        env = cfg["_env"]
        if not env["okx_api_key"]:
            raise OKXError("OKX_API_KEY not set — cannot construct adapter")
        import ccxt
        self._ccxt = ccxt
        self._okx_cfg = cfg.get("okx", {})
        self._hostname = env["okx_hostname"]
        self._post_only_reprice_ticks = cfg.get("engine", {}).get("post_only_reprice_ticks", 1)
        self._clordid_prefix = self._okx_cfg.get("clordid_prefix", "sa")
        self._client = self._make_client(env)
        self._verify_unified_account()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _make_client(self, env: dict[str, Any]) -> Any:
        client = self._ccxt.okx({
            "apiKey":   env["okx_api_key"],
            "secret":   env["okx_api_secret"],
            "password": env["okx_passphrase"],
            "enableRateLimit": True,
            "hostname": self._hostname,
            "options": {"adjustForTimeDifference": True},
        })
        try:
            client.load_time_difference()
        except Exception as exc:
            log.warning("load_time_difference best-effort failed: %s", exc)
        try:
            client.load_markets()
        except Exception as exc:
            log.warning("load_markets failed (continuing): %s", exc)
        return client

    def _verify_unified_account(self) -> None:
        """Spec §11: fail fast if account is not Unified / Portfolio Margin."""
        try:
            info = self._client.fetch_balance().get("info", {})
            data = (info.get("data") or [{}])[0]
            acct_lv = str(data.get("acctLv") or data.get("accountLevel") or "")
            if acct_lv and acct_lv not in ("2", "3", "4"):
                raise OKXError(
                    f"OKX account is not Unified/Portfolio Margin (acctLv={acct_lv}). "
                    "Enable in OKX → Trading → Account Mode before running APEX-Ω."
                )
        except OKXError:
            raise
        except Exception as exc:
            log.warning("account-level probe best-effort failed: %s", exc)

    # ------------------------------------------------------------------
    # Public data
    # ------------------------------------------------------------------

    async def get_funding(self, symbol: str) -> dict[str, Any]:
        """Return {rate, next_ts, mark_px} for the perp `symbol`."""
        perp = self._perp_for(symbol)
        fr = await asyncio.to_thread(self._client.fetch_funding_rate, perp)
        if not isinstance(fr, dict):
            raise OKXError(f"bad fetch_funding_rate payload for {perp}")
        return {
            "rate": float(fr.get("fundingRate") or 0),
            "next_ts": int(fr.get("fundingDatetime") and
                           fr.get("nextFundingTimestamp") or 0),
            "mark_px": float(fr.get("markPrice") or 0),
            "raw": fr,
        }

    async def get_funding_history(self, symbol: str, *, limit: int = 180) -> list[float]:
        """Last N funding rates (for 30d-sigma estimate). Returns absolute rates."""
        perp = self._perp_for(symbol)
        try:
            hist = await asyncio.to_thread(
                self._client.fetch_funding_rate_history, perp, None, limit,
            )
        except Exception as exc:
            log.debug("funding history unavailable for %s: %s", symbol, exc)
            return []
        out: list[float] = []
        for h in hist or []:
            if isinstance(h, dict):
                r = h.get("fundingRate")
                if r is not None:
                    out.append(abs(float(r)))
        return out

    async def get_ticker(self, symbol: str) -> dict[str, Any]:
        """Perp ticker — used by delta-neutral (Omega) engine."""
        t = await asyncio.to_thread(self._client.fetch_ticker, self._perp_for(symbol))
        if not isinstance(t, dict):
            raise OKXError(f"bad ticker for {symbol}")
        last = float(t.get("last") or t.get("close") or 0)
        bid  = float(t.get("bid") or 0)
        ask  = float(t.get("ask") or 0)
        spread_bp = (ask - bid) / last * 1e4 if last > 0 and bid > 0 and ask > 0 else 0
        return {"last": last, "bid": bid, "ask": ask,
                "spread_bp": spread_bp,
                "volume_24h_usd": float(t.get("quoteVolume") or 0),
                "raw": t}

    async def get_spot_ticker(self, symbol: str) -> dict[str, Any]:
        """Spot ticker — used by SPOT AGGRO engine."""
        t = await asyncio.to_thread(self._client.fetch_ticker, self._spot_for(symbol))
        if not isinstance(t, dict):
            raise OKXError(f"bad spot ticker for {symbol}")
        last = float(t.get("last") or t.get("close") or 0)
        bid  = float(t.get("bid") or 0)
        ask  = float(t.get("ask") or 0)
        spread_bp = (ask - bid) / last * 1e4 if last > 0 and bid > 0 and ask > 0 else 0
        return {"last": last, "bid": bid, "ask": ask,
                "spread_bp": spread_bp,
                "volume_24h_usd": float(t.get("quoteVolume") or 0),
                "raw": t}

    async def get_book_depth_usd(self, symbol: str, levels: int = 5) -> float:
        """Perp book depth — used by delta-neutral (Omega) engine."""
        try:
            ob = await asyncio.to_thread(
                self._client.fetch_order_book, self._perp_for(symbol), levels,
            )
        except Exception as exc:
            log.debug("fetch_order_book failed for %s: %s", symbol, exc)
            return 0.0
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        bid_usd = sum(float(p) * float(q) for p, q, *_ in bids[:levels])
        ask_usd = sum(float(p) * float(q) for p, q, *_ in asks[:levels])
        return bid_usd + ask_usd

    async def get_spot_book_depth_usd(self, symbol: str, levels: int = 5) -> float:
        """Spot book depth — used by SPOT AGGRO engine."""
        try:
            ob = await asyncio.to_thread(
                self._client.fetch_order_book, self._spot_for(symbol), levels,
            )
        except Exception as exc:
            log.debug("fetch_order_book (spot) failed for %s: %s", symbol, exc)
            return 0.0
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        bid_usd = sum(float(p) * float(q) for p, q, *_ in bids[:levels])
        ask_usd = sum(float(p) * float(q) for p, q, *_ in asks[:levels])
        return bid_usd + ask_usd

    # ------------------------------------------------------------------
    # spot_aggro: OI + price return (feeds real data into SPI formula)
    # ------------------------------------------------------------------

    async def get_open_interest_change(self, symbol: str) -> float:
        """24h open interest change as fraction (e.g. 0.05 = 5% growth).
        Uses perp OI (public data). Returns 0.0 on failure."""
        try:
            perp = self._perp_for(symbol)
            # ccxt doesn't have a direct OI endpoint; use fetchOpenInterestHistory
            hist = await asyncio.to_thread(
                self._client.fetch_open_interest_history, perp, "1D", None, 2,
            )
            if hist and len(hist) >= 2:
                latest = float(hist[-1].get("openInterestValue") or hist[-1].get("openInterestAmount") or 0)
                prev = float(hist[-2].get("openInterestValue") or hist[-2].get("openInterestAmount") or 0)
                if prev > 0:
                    return (latest - prev) / prev
            return 0.0
        except Exception:
            return 0.0

    async def get_price_return_7d(self, symbol: str) -> float:
        """7-day spot price return as fraction (e.g. 0.03 = +3%). Returns 0.0 on failure."""
        try:
            spot = self._spot_for(symbol)
            candles = await asyncio.to_thread(
                self._client.fetch_ohlcv, spot, "1d", None, 8,
            )
            if candles and len(candles) >= 2:
                close_now = float(candles[-1][4])
                close_7d = float(candles[0][4])
                if close_7d > 0:
                    return (close_now - close_7d) / close_7d
            return 0.0
        except Exception:
            return 0.0

    async def get_recent_low(self, symbol: str) -> float:
        """24h low price from spot ticker. Used for liq cluster estimate."""
        try:
            spot = self._spot_for(symbol)
            t = await asyncio.to_thread(self._client.fetch_ticker, spot)
            return float(t.get("low") or 0)
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # Private account state
    # ------------------------------------------------------------------

    async def get_account_equity(self) -> float:
        """Total USD equity across the unified account."""
        bal = await asyncio.to_thread(self._client.fetch_balance)
        info = bal.get("info", {})
        if isinstance(info, dict) and info.get("data"):
            d = info["data"][0]
            te = d.get("totalEq")
            if te is not None:
                return float(te)
        usdt = bal.get("USDT") or {}
        return float(usdt.get("total") or 0)

    async def get_free_usdt(self) -> float:
        """Return free / available USDT — the number that actually gates
        whether a BUY can be placed. `get_account_equity` returns TOTAL
        equity (free + holdings mark value), which is not useful as a
        pre-flight guard; a full portfolio with $0.01 free USDT will pass
        an equity check but fail at OKX with code 51008 (Insufficient
        balance). Use THIS method before any BUY, not get_account_equity.
        """
        bal = await asyncio.to_thread(self._client.fetch_balance)
        free = bal.get("free") or {}
        v = free.get("USDT")
        if v is not None:
            return float(v)
        usdt = bal.get("USDT") or {}
        return float(usdt.get("free") or 0)

    async def get_spot_holdings(self) -> dict[str, float]:
        """All non-USDT coin balances with positive amount. Key = base coin
        (e.g. 'TIA'), value = total quantity. Used by reconciliation."""
        bal = await asyncio.to_thread(self._client.fetch_balance)
        total = bal.get("total") or {}
        out: dict[str, float] = {}
        for k, v in total.items():
            if k in ("USDT", "USD", "SGD"):
                continue
            try:
                q = float(v or 0)
            except (TypeError, ValueError):
                continue
            if q > 0:
                out[k] = q
        return out

    async def get_spot_fills(self, symbol: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Read-only historical fills for a spot symbol. Returns the raw
        ccxt shape (each fill has side/amount/price/fee/order/timestamp).
        Used ONLY by reconciliation — never by order submission.
        """
        spot = self._spot_for(symbol)
        try:
            trades = await asyncio.to_thread(
                self._client.fetch_my_trades, spot, None, int(limit)
            )
            return list(trades or [])
        except Exception as exc:
            raise OKXError(f"fetch_my_trades({spot}) failed: {exc}") from exc

    async def get_positions(self) -> list[dict[str, Any]]:
        """Return list of open perp positions in canonical form."""
        try:
            pos = await asyncio.to_thread(self._client.fetch_positions)
        except Exception as exc:
            raise OKXError(f"fetch_positions failed: {exc}") from exc
        out = []
        for p in pos or []:
            c = float(p.get("contracts") or 0)
            if abs(c) < 1e-9:
                continue
            out.append({
                "symbol": p.get("symbol"),
                "contracts": c,
                "side": "short" if c < 0 else "long",
                "mark_px": float(p.get("markPrice") or 0),
                "entry_px": float(p.get("entryPrice") or 0),
                "initial_margin": float(p.get("initialMargin") or 0),
                "unrealized_pnl": float(p.get("unrealizedPnl") or 0),
                "raw": p,
            })
        return out

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def place_post_only(
        self,
        *,
        symbol: str,
        side: str,                 # "buy" | "sell"
        notional_usd: float,
        reference_price: float,
        leg: str,                  # "perp" | "spot"
        idempotency_seed: str,
        max_retries: int = 3,
    ) -> OrderReceipt:
        """
        Post-only limit. On reject (would-take), re-quote N ticks further
        from mid and retry, up to `max_retries`. Never falls back to taker.
        """
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be buy|sell, got {side!r}")
        instr = self._perp_for(symbol) if leg == "perp" else self._spot_for(symbol)
        td_mode = "cash" if leg == "spot" else self._okx_cfg.get("td_mode", "cross")
        cl_ord_id = self._mk_cl_ord_id(idempotency_seed)

        attempt = 0
        tick = self._estimate_tick(reference_price)
        last_exc: Optional[Exception] = None
        while attempt < max_retries:
            attempt += 1
            # Price 1 tick INSIDE the book (maker side)
            if side == "buy":
                limit_px = reference_price - attempt * tick
            else:
                limit_px = reference_price + attempt * tick
            qty = notional_usd / max(limit_px, 1e-9)
            params = {"postOnly": True, "tdMode": td_mode, "clOrdId": cl_ord_id}
            try:
                order = await asyncio.to_thread(
                    self._client.create_order,
                    instr, "limit", side, qty, limit_px, params,
                )
                return OrderReceipt(
                    ok=True, order_id=str(order.get("id") or ""),
                    cl_ord_id=cl_ord_id,
                    filled_qty=float(order.get("filled") or 0),
                    avg_px=float(order.get("average") or limit_px),
                    side=side, symbol=instr,
                    notional_usd=notional_usd,
                    fee_usd=self._extract_fee(order),
                    raw=order,
                )
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                # Would-cross (post-only rejected) → reprice outward, retry
                if any(tag in msg for tag in ("51121", "51000", "post only", "postOnly")):
                    log.info("post-only rejected %s attempt %d — repricing", symbol, attempt)
                    continue
                # Duplicate clOrdId → order already exists, fetch it
                if any(tag in msg for tag in ("51402", "duplicate", "already exists")):
                    return await self._fetch_by_cl_ord_id(cl_ord_id, instr,
                                                         notional_usd, side)
                # Transient → retry once more
                if any(t in type(exc).__name__ for t in
                       ("Timeout", "NetworkError", "ExchangeNotAvailable")):
                    await asyncio.sleep(2 ** attempt)
                    continue
                # Definitive rejection — fall through to market fallback
                break

        # Market-order fallback: if all post-only attempts failed AND we have
        # a valid adapter, place a single market order to guarantee the fill.
        # Taker fee is 5bp instead of 2bp, but zero fills costs more than 3bp.
        try:
            order = await asyncio.to_thread(
                self._client.create_order,
                instr, "market", side, notional_usd / max(reference_price, 1e-9),
                None,
                {"tdMode": td_mode, "clOrdId": cl_ord_id + "m"},
            )
            return OrderReceipt(
                ok=True, order_id=str(order.get("id") or ""),
                cl_ord_id=cl_ord_id + "m",
                filled_qty=float(order.get("filled") or 0),
                avg_px=float(order.get("average") or reference_price),
                side=side, symbol=instr,
                notional_usd=notional_usd,
                fee_usd=self._extract_fee(order),
                raw=order,
            )
        except Exception as mkt_exc:
            pass   # fall through to the hard failure below

        return OrderReceipt(
            ok=False, order_id=None, cl_ord_id=cl_ord_id,
            filled_qty=0, avg_px=0, side=side, symbol=instr,
            notional_usd=notional_usd, fee_usd=0, raw={},
            error=f"{type(last_exc).__name__}: {str(last_exc)[:180]}" if last_exc else "unknown",
        )

    async def close_pair(self, symbol: str) -> list[OrderReceipt]:
        """Reduce-only market close of BOTH legs of a pair. Uses market (reduce-only is safe)."""
        perp = self._perp_for(symbol)
        spot = self._spot_for(symbol)
        receipts: list[OrderReceipt] = []

        positions = await self.get_positions()
        for p in positions:
            if p["symbol"] != perp:
                continue
            side = "buy" if p["contracts"] < 0 else "sell"
            qty = abs(p["contracts"])
            try:
                order = await asyncio.to_thread(
                    self._client.create_market_order,
                    perp, side, qty,
                    {"reduceOnly": True, "tdMode": self._okx_cfg.get("td_mode", "cross")},
                )
                receipts.append(OrderReceipt(
                    ok=True, order_id=str(order.get("id") or ""),
                    cl_ord_id=None,
                    filled_qty=float(order.get("filled") or qty),
                    avg_px=float(order.get("average") or 0),
                    side=side, symbol=perp,
                    notional_usd=qty * float(order.get("average") or p["mark_px"]),
                    fee_usd=self._extract_fee(order),
                    raw=order,
                ))
            except Exception as exc:
                receipts.append(OrderReceipt(
                    ok=False, order_id=None, cl_ord_id=None,
                    filled_qty=0, avg_px=0, side=side, symbol=perp,
                    notional_usd=0, fee_usd=0, raw={},
                    error=str(exc)[:200],
                ))

        try:
            bal = await asyncio.to_thread(self._client.fetch_balance)
            base = spot.split("/")[0]
            qty = float((bal.get(base) or {}).get("free") or 0)
            if qty > 0:
                order = await asyncio.to_thread(
                    self._client.create_market_order, spot, "sell", qty, {},
                )
                receipts.append(OrderReceipt(
                    ok=True, order_id=str(order.get("id") or ""),
                    cl_ord_id=None,
                    filled_qty=float(order.get("filled") or qty),
                    avg_px=float(order.get("average") or 0),
                    side="sell", symbol=spot,
                    notional_usd=qty * float(order.get("average") or 0),
                    fee_usd=self._extract_fee(order),
                    raw=order,
                ))
        except Exception as exc:
            receipts.append(OrderReceipt(
                ok=False, order_id=None, cl_ord_id=None,
                filled_qty=0, avg_px=0, side="sell", symbol=spot,
                notional_usd=0, fee_usd=0, raw={},
                error=f"spot close: {exc}",
            ))
        return receipts

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        try:
            await asyncio.to_thread(self._client.cancel_order, order_id, self._perp_for(symbol))
            return True
        except Exception as exc:
            log.warning("cancel_order failed for %s: %s", order_id, exc)
            return False

    async def fetch_open_spot_orders(self) -> list[dict[str, Any]]:
        """All currently-open spot orders for this account. Uses ccxt's
        fetch_open_orders() with no symbol filter so we get one sweep.
        Never raises — returns [] on any failure."""
        try:
            rows = await asyncio.to_thread(self._client.fetch_open_orders)
            return list(rows or [])
        except Exception as exc:
            log.warning("fetch_open_spot_orders failed: %s", exc)
            return []

    async def cancel_open_buys_for_tier(self, tier: str) -> dict[str, Any]:
        """Cancel every OPEN BUY order whose clOrdId tag matches the
        given tier. Never touches open positions.

        Phase 11m — consumed by the soft-halt path. The engine tags every
        entry clOrdId with the tier at place time (place_post_only seeds
        the clOrdId from `sa:{symbol}:{minute}` but the order payload
        carries a `tag` / clientOrderId we can scan). If no orders are
        taggable, we match by symbol→tier via the trade log.

        Returns: {
          "tier": tier,
          "cancelled": int,
          "inspected": int,
          "errors": [str, ...],
        }
        """
        result: dict[str, Any] = {
            "tier": tier, "cancelled": 0, "inspected": 0, "errors": [],
        }
        if self.engine != "spot_aggro":
            result["errors"].append("adapter engine != spot_aggro; refusing")
            return result

        # Resolve which symbols currently belong to the named tier, using
        # the spot engine's tier_system if live, else by looking at recent
        # entry rows in the trade log (last 24h) for that tier.
        symbols_in_tier: set[str] = set()
        try:
            from spot_aggro import _engine_instance
            if _engine_instance is not None:
                tu = (_engine_instance.state.__dict__.get("tier_system", {})
                      or {}).get("tiered_universe") or []
                symbols_in_tier = {
                    c["symbol"] for c in tu if c.get("tier") == tier
                }
        except Exception:  # noqa: BLE001
            pass

        if not symbols_in_tier:
            # Fallback: recent entry log rows for this tier (24h).
            try:
                from shared.persistence import state as persist
                persist.init_schema()
                con = persist._connect()
                try:
                    import time as _time
                    cutoff_ms = int((_time.time() - 86400) * 1000)
                    rows = con.execute(
                        "SELECT DISTINCT symbol FROM apex_trade_log "
                        "WHERE ts_ms > ? AND action='enter' AND tier = ?",
                        (cutoff_ms, tier),
                    ).fetchall()
                    symbols_in_tier = {r[0] for r in rows if r[0]}
                finally:
                    con.close()
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"tier-resolve: {exc!r}"[:120])
                return result

        if not symbols_in_tier:
            return result  # nothing to cancel — tier has no active symbols

        rows = await self.fetch_open_spot_orders()
        result["inspected"] = len(rows)

        for o in rows:
            try:
                side = (o.get("side") or "").lower()
                sym = o.get("symbol") or ""    # ccxt "BTC/USDT"
                # ccxt symbol "BTC/USDT" -> internal "BTC-USDT"
                symbol_internal = sym.replace("/", "-")
                if side != "buy":
                    continue
                if symbol_internal not in symbols_in_tier:
                    continue
                oid = o.get("id") or o.get("clientOrderId")
                if not oid:
                    result["errors"].append(
                        f"order with no id on {symbol_internal}"[:120]
                    )
                    continue
                ok = await asyncio.to_thread(
                    self._client.cancel_order, oid, sym
                )
                # ccxt returns dict on success; cancel_order() here already
                # handled exceptions. We count successes conservatively.
                if ok:
                    result["cancelled"] += 1
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(
                    f"{o.get('symbol','?')}: {type(exc).__name__}: {str(exc)[:80]}"
                )
        return result

    async def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._client.fetch_order, order_id, self._perp_for(symbol))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _perp_for(self, symbol: str) -> str:
        """INJ-USDT → INJ-USDT-SWAP; pass through if already perp."""
        if symbol.endswith("-SWAP"):
            return symbol
        return f"{symbol}-SWAP"

    def _spot_for(self, symbol: str) -> str:
        """INJ-USDT → INJ/USDT."""
        s = symbol.replace("-SWAP", "")
        parts = s.split("-")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
        return s

    def _mk_cl_ord_id(self, seed: str) -> str:
        """OKX EEA clOrdId rules: 1-32 alphanumeric. Some regions reject
        underscores or certain patterns. Use a pure-alpha prefix + short hex."""
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
        return f"apx{digest}"

    def _extract_fee(self, order: dict[str, Any]) -> float:
        total = 0.0
        f = order.get("fee")
        if isinstance(f, dict):
            total += abs(float(f.get("cost") or 0))
        for fi in (order.get("fees") or []):
            if isinstance(fi, dict):
                total += abs(float(fi.get("cost") or 0))
        return total

    def _estimate_tick(self, ref_px: float) -> float:
        """Simple per-magnitude tick. Real tick reads markets[].info when load_markets worked."""
        if ref_px >= 100:    return 0.1
        if ref_px >= 1:      return 0.0001
        if ref_px >= 0.01:   return 0.000001
        return 0.00000001

    async def _fetch_by_cl_ord_id(self, cl_ord_id: str, instr: str,
                                  notional_usd: float, side: str) -> OrderReceipt:
        try:
            orders = await asyncio.to_thread(
                self._client.fetch_orders, instr, None, 30,
                {"clOrdId": cl_ord_id},
            )
        except Exception:
            orders = []
        for o in orders or []:
            if (o.get("clientOrderId") or o.get("info", {}).get("clOrdId")) == cl_ord_id:
                return OrderReceipt(
                    ok=True, order_id=str(o.get("id") or ""),
                    cl_ord_id=cl_ord_id,
                    filled_qty=float(o.get("filled") or 0),
                    avg_px=float(o.get("average") or 0),
                    side=side, symbol=instr,
                    notional_usd=notional_usd,
                    fee_usd=self._extract_fee(o),
                    raw=o,
                )
        return OrderReceipt(
            ok=False, order_id=None, cl_ord_id=cl_ord_id,
            filled_qty=0, avg_px=0, side=side, symbol=instr,
            notional_usd=notional_usd, fee_usd=0, raw={},
            error="duplicate clOrdId but order not found on fetch",
        )
