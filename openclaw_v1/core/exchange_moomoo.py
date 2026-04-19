"""
Moomoo / Futu adapter — standalone, additive.

Moomoo is NOT a CCXT exchange. `ccxt.exchanges` does not contain "moomoo"
and there is no CCXT id that maps to it, so setting `EXCHANGE_ID=moomoo`
in the trading bot's .env will fail at import. Moomoo trading uses its
own stack:

  Local daemon   :  OpenD (downloaded from https://openapi.futunn.com)
  Python SDK     :  moomoo-api  (pip install moomoo-api) or
                    futu-api    (same interface, Asian branding)

This module provides a drop-in replacement for `core.exchange.ExecutionEngine`
that speaks Moomoo OpenD instead of CCXT. It wraps the SDK's callback-based
subscriptions behind an async iterator interface shaped like `ccxt.pro`, so
`main.py`'s existing `await engine.exchange.watch_trades(symbol)` pattern
keeps working when you swap the engine in.

Required env vars (set these in the panel's Settings card or directly in
`openclaw_v1/config/.env`):

  MOOMOO_HOST             default 127.0.0.1
  MOOMOO_PORT             default 11111
  MOOMOO_TRADE_PASSWORD   plaintext trade password, OR
  MOOMOO_TRADE_PWD_MD5    hex MD5 of the trade password (preferred)
  MOOMOO_ACCOUNT_ID       optional; first eligible account is used otherwise
  MOOMOO_MARKET           HK | US | CN | SG
  MOOMOO_TRADE_ENV        REAL | SIMULATE
  MOOMOO_RSA_PRIVATE_KEY  optional PEM or file path for encrypted OpenD
  MOOMOO_OPENAPI_KEY      only if using cloud OpenAPI (rare)
  MOOMOO_OPENAPI_SECRET   paired secret for MOOMOO_OPENAPI_KEY

Symbol format: Moomoo uses "HK.00700", "US.AAPL", "SZ.000001", etc.
Pass symbols in that form to `watch_trades` / `watch_order_book` /
`fetch_ohlcv` / `create_order`. There is no "BTC/USDT" style here — if
you want crypto, keep the existing CCXT adapter for that feed.

Wiring (one-line change in main.py — do this yourself when you are ready,
this file does not touch main.py):

    # before
    from core.exchange import ExecutionEngine
    engine = ExecutionEngine(config.exchange.id, api_key, secret)

    # after (only when config.exchange.id == "moomoo")
    if config.exchange.id == "moomoo":
        from core.exchange_moomoo import MoomooExecutionEngine
        engine = MoomooExecutionEngine()
    else:
        from core.exchange import ExecutionEngine
        engine = ExecutionEngine(config.exchange.id, api_key, secret)

Nothing in this file imports from `core.exchange` or from `main.py`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _load_moomoo_sdk():
    """Lazily import moomoo-api (or futu-api, same interface) so the module
    is importable even on machines that have not run `pip install moomoo-api`
    yet. Raises a helpful error on first real use, not on import."""
    try:
        import moomoo as _ft
    except ImportError:
        try:
            import futu as _ft  # fallback — futu-api ships the same symbols
        except ImportError as exc:
            raise ImportError(
                "Moomoo adapter requires `moomoo-api` (or `futu-api`). "
                "Install with: pip install moomoo-api"
            ) from exc
    return _ft


def _md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


class _AsyncQueueHandler:
    """Base class for moomoo handlers that dump incoming callback data into
    an asyncio.Queue so consumers can `await queue.get()`. The moomoo SDK
    invokes handlers on a background thread; we bridge into the event loop
    via `loop.call_soon_threadsafe(queue.put_nowait, payload)`."""

    def __init__(self, loop: asyncio.AbstractEventLoop, ft_module: Any):
        self._loop = loop
        self._ft = ft_module
        self.queue: "asyncio.Queue[Any]" = asyncio.Queue()


class _TradeHandler(_AsyncQueueHandler):
    def on_recv_rsp(self, rsp_pb):  # called by moomoo-api thread
        ret, data = self._ft.TickerHandlerBase.on_recv_rsp(self, rsp_pb)
        if ret == self._ft.RET_OK:
            self._loop.call_soon_threadsafe(self.queue.put_nowait, data)
        else:
            logger.warning("moomoo trade feed error: %s", data)
        return ret, data


class _OrderBookHandler(_AsyncQueueHandler):
    def on_recv_rsp(self, rsp_pb):
        ret, data = self._ft.OrderBookHandlerBase.on_recv_rsp(self, rsp_pb)
        if ret == self._ft.RET_OK:
            self._loop.call_soon_threadsafe(self.queue.put_nowait, data)
        else:
            logger.warning("moomoo orderbook error: %s", data)
        return ret, data


class _KLineHandler(_AsyncQueueHandler):
    def on_recv_rsp(self, rsp_pb):
        ret, data = self._ft.CurKlineHandlerBase.on_recv_rsp(self, rsp_pb)
        if ret == self._ft.RET_OK:
            self._loop.call_soon_threadsafe(self.queue.put_nowait, data)
        else:
            logger.warning("moomoo kline error: %s", data)
        return ret, data


class MoomooExchange:
    """CCXT-Pro-shaped façade over moomoo-api. Only the methods used by
    main.py are implemented: watch_trades, watch_order_book, fetch_ohlcv,
    watch_ohlcv, create_order. All other CCXT methods are intentionally
    absent — you should not rely on them through this adapter."""

    def __init__(self) -> None:
        self._ft = _load_moomoo_sdk()
        self._host = os.getenv("MOOMOO_HOST", "127.0.0.1")
        self._port = int(os.getenv("MOOMOO_PORT", "11111"))
        self._market = os.getenv("MOOMOO_MARKET", "HK").upper()
        self._trade_env = os.getenv("MOOMOO_TRADE_ENV", "SIMULATE").upper()
        self._account_id = os.getenv("MOOMOO_ACCOUNT_ID") or None

        pwd_md5 = os.getenv("MOOMOO_TRADE_PWD_MD5")
        pwd_plain = os.getenv("MOOMOO_TRADE_PASSWORD")
        self._trade_pwd_md5 = pwd_md5 or (_md5(pwd_plain) if pwd_plain else None)

        self._rsa = os.getenv("MOOMOO_RSA_PRIVATE_KEY") or None

        self._quote_ctx = None
        self._trade_ctx = None
        self._subscribed: set[tuple[str, str]] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._trade_queues: dict[str, asyncio.Queue] = {}
        self._book_queues: dict[str, asyncio.Queue] = {}
        self._kline_queues: dict[tuple[str, str], asyncio.Queue] = {}

    # ---- connection ----------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return self._loop

    def _ensure_quote_ctx(self):
        if self._quote_ctx is None:
            kwargs: dict[str, Any] = {"host": self._host, "port": self._port}
            if self._rsa:
                kwargs["is_encrypt"] = True
                # moomoo-api reads the key via SysConfig — caller should set
                # the RSA file path ahead of time if MOOMOO_RSA_PRIVATE_KEY
                # is a path. If it is an inline PEM, the caller can set it
                # via ft.SysConfig.set_init_rsa_file(...).
            self._quote_ctx = self._ft.OpenQuoteContext(**kwargs)
        return self._quote_ctx

    def _ensure_trade_ctx(self):
        if self._trade_ctx is None:
            market_cls = {
                "HK": self._ft.OpenHKTradeContext,
                "US": self._ft.OpenUSTradeContext,
                "CN": self._ft.OpenCNTradeContext,
                "SG": self._ft.OpenSGTradeContext,
            }.get(self._market)
            if market_cls is None:
                raise ValueError(f"Unsupported MOOMOO_MARKET: {self._market}")
            self._trade_ctx = market_cls(host=self._host, port=self._port)
            if self._trade_pwd_md5:
                ret, data = self._trade_ctx.unlock_trade(
                    password_md5=self._trade_pwd_md5
                )
                if ret != self._ft.RET_OK:
                    raise RuntimeError(f"Moomoo unlock_trade failed: {data}")
            else:
                logger.warning(
                    "MOOMOO_TRADE_PASSWORD / MOOMOO_TRADE_PWD_MD5 not set — "
                    "trade context can observe but not place orders."
                )
        return self._trade_ctx

    async def close(self) -> None:
        if self._quote_ctx is not None:
            self._quote_ctx.close()
            self._quote_ctx = None
        if self._trade_ctx is not None:
            self._trade_ctx.close()
            self._trade_ctx = None

    # ---- data feeds ----------------------------------------------------

    async def watch_trades(self, symbol: str):
        """Return the next trade batch for `symbol`. Idempotent subscribe."""
        loop = self._ensure_loop()
        ctx = self._ensure_quote_ctx()
        if ("trade", symbol) not in self._subscribed:
            handler = _TradeHandler(loop, self._ft)
            ctx.set_handler(handler)
            ret, data = ctx.subscribe([symbol], [self._ft.SubType.TICKER])
            if ret != self._ft.RET_OK:
                raise RuntimeError(f"subscribe(TICKER, {symbol}) failed: {data}")
            self._trade_queues[symbol] = handler.queue
            self._subscribed.add(("trade", symbol))
        return await self._trade_queues[symbol].get()

    async def watch_order_book(self, symbol: str):
        loop = self._ensure_loop()
        ctx = self._ensure_quote_ctx()
        if ("book", symbol) not in self._subscribed:
            handler = _OrderBookHandler(loop, self._ft)
            ctx.set_handler(handler)
            ret, data = ctx.subscribe([symbol], [self._ft.SubType.ORDER_BOOK])
            if ret != self._ft.RET_OK:
                raise RuntimeError(f"subscribe(ORDER_BOOK, {symbol}) failed: {data}")
            self._book_queues[symbol] = handler.queue
            self._subscribed.add(("book", symbol))
        return await self._book_queues[symbol].get()

    async def watch_ohlcv(self, symbol: str, timeframe: str):
        loop = self._ensure_loop()
        ctx = self._ensure_quote_ctx()
        ktype = self._timeframe_to_ktype(timeframe)
        key = (symbol, ktype)
        if ("kline", symbol, ktype) not in self._subscribed:
            handler = _KLineHandler(loop, self._ft)
            ctx.set_handler(handler)
            ret, data = ctx.subscribe([symbol], [ktype])
            if ret != self._ft.RET_OK:
                raise RuntimeError(f"subscribe({ktype}, {symbol}) failed: {data}")
            self._kline_queues[key] = handler.queue
            self._subscribed.add(("kline", symbol, ktype))
        return await self._kline_queues[key].get()

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 500):
        ctx = self._ensure_quote_ctx()
        ktype = self._timeframe_to_ktype(timeframe)
        # moomoo-api is sync; run in default executor so we don't block the loop
        loop = self._ensure_loop()
        ret, data, _page_key = await loop.run_in_executor(
            None,
            lambda: ctx.request_history_kline(
                symbol,
                ktype=ktype,
                max_count=limit,
            ),
        )
        if ret != self._ft.RET_OK:
            raise RuntimeError(f"request_history_kline({symbol}, {ktype}) failed: {data}")
        return data

    def _timeframe_to_ktype(self, timeframe: str) -> str:
        """Map CCXT timeframe strings ("1m", "5m", "1h", "1d") to moomoo KType."""
        mapping = {
            "1m": self._ft.KLType.K_1M,
            "3m": self._ft.KLType.K_3M,
            "5m": self._ft.KLType.K_5M,
            "15m": self._ft.KLType.K_15M,
            "30m": self._ft.KLType.K_30M,
            "1h": self._ft.KLType.K_60M,
            "1d": self._ft.KLType.K_DAY,
            "1w": self._ft.KLType.K_WEEK,
            "1M": self._ft.KLType.K_MON,
        }
        if timeframe not in mapping:
            raise ValueError(f"Unsupported timeframe for moomoo: {timeframe}")
        return mapping[timeframe]

    # ---- trading -------------------------------------------------------

    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
    ):
        """side is 'buy' or 'sell'; order_type is 'market' or 'limit'."""
        ctx = self._ensure_trade_ctx()
        trd_side = (
            self._ft.TrdSide.BUY if side.lower() == "buy" else self._ft.TrdSide.SELL
        )
        if order_type == "market":
            trd_order_type = self._ft.OrderType.MARKET
            order_price = 0.0
        elif order_type == "limit":
            if price is None:
                raise ValueError("limit orders require a price")
            trd_order_type = self._ft.OrderType.NORMAL
            order_price = float(price)
        else:
            raise ValueError(f"Unsupported order_type: {order_type}")

        trd_env = (
            self._ft.TrdEnv.REAL
            if self._trade_env == "REAL"
            else self._ft.TrdEnv.SIMULATE
        )

        loop = self._ensure_loop()
        ret, data = await loop.run_in_executor(
            None,
            lambda: ctx.place_order(
                price=order_price,
                qty=amount,
                code=symbol,
                trd_side=trd_side,
                order_type=trd_order_type,
                trd_env=trd_env,
                acc_id=int(self._account_id) if self._account_id else 0,
            ),
        )
        if ret != self._ft.RET_OK:
            raise RuntimeError(f"place_order failed: {data}")
        logger.info("moomoo order placed: %s", data)
        return data


class MoomooExecutionEngine:
    """Drop-in replacement for core.exchange.ExecutionEngine. No constructor
    arguments — all configuration comes from env vars so main.py does not
    have to change its call site."""

    def __init__(self) -> None:
        self.exchange = MoomooExchange()

    async def watch_orderbook(self, symbol: str) -> None:
        while True:
            try:
                ob = await self.exchange.watch_order_book(symbol)
                bids = getattr(ob, "Bid", None) or getattr(ob, "bids", None)
                asks = getattr(ob, "Ask", None) or getattr(ob, "asks", None)
                if bids and asks:
                    logger.info(
                        "[%s] Best Bid: %s | Best Ask: %s",
                        symbol,
                        bids[0][0] if bids else None,
                        asks[0][0] if asks else None,
                    )
                await asyncio.sleep(0.01)
            except Exception as exc:
                logger.error("moomoo watch_orderbook error: %s", exc)
                await asyncio.sleep(1)

    async def execute_trade(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
    ):
        try:
            order_type = "limit" if price else "market"
            order = await self.exchange.create_order(
                symbol, order_type, side, amount, price
            )
            logger.info("EXECUTION CONFIRMED: %s", order)
            return order
        except Exception as exc:
            logger.error("Execution Failure: %s", exc)
            return None
