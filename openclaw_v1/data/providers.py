"""
Thin async HTTP clients for the external data sources OpenClaw depends on.

Each client reads its credential from config/.env. Methods on each client are
illustrative entry points — extend them as you wire more endpoints into the
decision engine. Remember to await `close()` on each client before shutdown.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import aiohttp


class _BaseClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _get(
        self,
        path: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> Any:
        session = await self._get_session()
        async with session.get(
            f"{self.base_url}{path}", params=params, headers=headers
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _post(
        self,
        path: str,
        json: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> Any:
        session = await self._get_session()
        async with session.post(
            f"{self.base_url}{path}", json=json, headers=headers
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# ---------- Market data ----------


class CoinGeckoClient(_BaseClient):
    def __init__(self) -> None:
        super().__init__("https://api.coingecko.com/api/v3")
        self.api_key = os.getenv("COINGECKO_API_KEY", "")

    def _headers(self) -> Optional[dict]:
        return {"x-cg-demo-api-key": self.api_key} if self.api_key else None

    async def simple_price(self, ids: list, vs: str = "usd") -> dict:
        return await self._get(
            "/simple/price",
            params={"ids": ",".join(ids), "vs_currencies": vs},
            headers=self._headers(),
        )

    async def market_chart(self, coin_id: str, vs: str = "usd", days: int = 1) -> dict:
        return await self._get(
            f"/coins/{coin_id}/market_chart",
            params={"vs_currency": vs, "days": days},
            headers=self._headers(),
        )


class CryptoCompareClient(_BaseClient):
    def __init__(self) -> None:
        super().__init__("https://min-api.cryptocompare.com/data")
        self.api_key = os.getenv("CRYPTOCOMPARE_API_KEY", "")

    async def histominute(self, fsym: str, tsym: str = "USD", limit: int = 60) -> dict:
        params: dict = {"fsym": fsym, "tsym": tsym, "limit": limit}
        if self.api_key:
            params["api_key"] = self.api_key
        return await self._get("/v2/histominute", params=params)


class CoinMarketCapClient(_BaseClient):
    def __init__(self) -> None:
        super().__init__("https://pro-api.coinmarketcap.com/v1")
        self.api_key = os.getenv("CMC_API_KEY", "")

    async def latest_quotes(self, symbols: list) -> dict:
        return await self._get(
            "/cryptocurrency/quotes/latest",
            params={"symbol": ",".join(symbols)},
            headers={
                "X-CMC_PRO_API_KEY": self.api_key,
                "Accept": "application/json",
            },
        )


# ---------- On-chain ----------


class GlassnodeClient(_BaseClient):
    def __init__(self) -> None:
        super().__init__("https://api.glassnode.com")
        self.api_key = os.getenv("GLASSNODE_API_KEY", "")

    async def metric(self, endpoint: str, asset: str = "BTC", **params: Any) -> Any:
        q = {"a": asset, "api_key": self.api_key, **params}
        return await self._get(f"/v1/metrics/{endpoint}", params=q)


class NansenClient(_BaseClient):
    def __init__(self) -> None:
        super().__init__("https://api.nansen.ai")
        self.api_key = os.getenv("NANSEN_API_KEY", "")

    async def smart_money_flows(self, chain: str = "ethereum") -> Any:
        return await self._get(
            f"/v1/smart-money/flows/{chain}",
            headers={"apiKey": self.api_key},
        )


class DuneClient(_BaseClient):
    def __init__(self) -> None:
        super().__init__("https://api.dune.com/api/v1")
        self.api_key = os.getenv("DUNE_API_KEY", "")

    async def execute_query(self, query_id: int, parameters: Optional[dict] = None) -> Any:
        return await self._post(
            f"/query/{query_id}/execute",
            json={"query_parameters": parameters or {}},
            headers={"X-Dune-API-Key": self.api_key},
        )

    async def query_results(self, execution_id: str) -> Any:
        return await self._get(
            f"/execution/{execution_id}/results",
            headers={"X-Dune-API-Key": self.api_key},
        )


class ArkhamClient(_BaseClient):
    def __init__(self) -> None:
        super().__init__("https://api.arkhamintelligence.com")
        self.api_key = os.getenv("ARKHAM_API_KEY", "")

    async def entity(self, address: str, chain: str = "ethereum") -> Any:
        return await self._get(
            f"/intelligence/address/{address}/{chain}",
            headers={"API-Key": self.api_key},
        )


class HeliusClient(_BaseClient):
    def __init__(self) -> None:
        self.api_key = os.getenv("HELIUS_API_KEY", "")
        super().__init__(f"https://mainnet.helius-rpc.com/?api-key={self.api_key}")

    async def rpc(self, method: str, params: Optional[list] = None) -> Any:
        return await self._post(
            "",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []},
        )

    async def get_balance(self, pubkey: str) -> Any:
        return await self.rpc("getBalance", [pubkey])


class AlchemyClient(_BaseClient):
    def __init__(self, network: str = "eth-mainnet") -> None:
        self.api_key = os.getenv("ALCHEMY_API_KEY", "")
        super().__init__(f"https://{network}.g.alchemy.com/v2/{self.api_key}")

    async def rpc(self, method: str, params: Optional[list] = None) -> Any:
        return await self._post(
            "",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []},
        )

    async def eth_block_number(self) -> Any:
        return await self.rpc("eth_blockNumber")


# ---------- Sentiment ----------


class NewsAPIClient(_BaseClient):
    def __init__(self) -> None:
        super().__init__("https://newsapi.org/v2")
        self.api_key = os.getenv("NEWS_API_KEY", "")

    async def everything(self, q: str, language: str = "en", page_size: int = 50) -> dict:
        return await self._get(
            "/everything",
            params={
                "q": q,
                "language": language,
                "pageSize": page_size,
                "apiKey": self.api_key,
            },
        )


class XClient(_BaseClient):
    """X (Twitter) v2 recent search."""

    def __init__(self) -> None:
        super().__init__("https://api.twitter.com/2")
        self.bearer = os.getenv("X_BEARER_TOKEN", "")

    async def recent_search(self, query: str, max_results: int = 50) -> dict:
        return await self._get(
            "/tweets/search/recent",
            params={"query": query, "max_results": max_results},
            headers={"Authorization": f"Bearer {self.bearer}"},
        )


class RedditClient(_BaseClient):
    """Reddit needs OAuth2 client-credentials flow to get a bearer token."""

    def __init__(self) -> None:
        super().__init__("https://oauth.reddit.com")
        self.client_id = os.getenv("REDDIT_CLIENT_ID", "")
        self.client_secret = os.getenv("REDDIT_CLIENT_SECRET", "")
        self.user_agent = os.getenv("REDDIT_USER_AGENT", "openclaw/0.1")
        self.token: Optional[str] = None

    async def authenticate(self) -> None:
        session = await self._get_session()
        auth = aiohttp.BasicAuth(self.client_id, self.client_secret)
        async with session.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=auth,
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": self.user_agent},
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
            self.token = payload["access_token"]

    async def subreddit_new(self, subreddit: str, limit: int = 50) -> dict:
        if not self.token:
            await self.authenticate()
        return await self._get(
            f"/r/{subreddit}/new",
            params={"limit": limit},
            headers={
                "Authorization": f"Bearer {self.token}",
                "User-Agent": self.user_agent,
            },
        )
