import ccxt.pro as ccxt
import asyncio
import logging

class ExecutionEngine:
    def __init__(self, exchange_id, api_key, api_secret):
        exchange_class = getattr(ccxt, exchange_id)
        self.exchange = exchange_class({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}
        })

    async def watch_orderbook(self, symbol):
        while True:
            try:
                orderbook = await self.exchange.watch_order_book(symbol)
                # Feed micro-structure changes to strategy engine
                logging.info(f"[{symbol}] Best Bid: {orderbook['bids'][0][0]} | Best Ask: {orderbook['asks'][0][0]}")
                await asyncio.sleep(0.01)
            except Exception as e:
                logging.error(f"WebSocket Error: {e}")
                await asyncio.sleep(1)

    async def execute_trade(self, symbol, side, amount, price=None):
        try:
            order_type = 'limit' if price else 'market'
            order = await self.exchange.create_order(symbol, order_type, side, amount, price)
            logging.info(f"EXECUTION CONFIRMED: {order}")
            return order
        except Exception as e:
            logging.error(f"Execution Failure: {e}")
