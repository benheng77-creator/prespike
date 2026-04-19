"""
Sentiment tracker that pulls news articles and tweets on a schedule and
scores each text with VADER. Exposes a `current()` snapshot of mean
polarity and coverage/freshness for the decision engine.

VADER is a lexicon-based baseline — fast, no model download, no GPU.
Swap for a transformer (e.g. ProsusAI/finbert or cardiffnlp/twitter-roberta)
or an LLM classifier when you want higher accuracy.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Tuple

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from data.providers import NewsAPIClient, XClient

log = logging.getLogger(__name__)


class SentimentTracker:
    def __init__(self, query: str, refresh_s: int = 300):
        self.query = query
        self.refresh_s = refresh_s
        self.analyzer = SentimentIntensityAnalyzer()

        self.news_client = NewsAPIClient() if os.getenv("NEWS_API_KEY") else None
        self.x_client = XClient() if os.getenv("X_BEARER_TOKEN") else None

        self._news_sent = 0.0
        self._social_sent = 0.0
        self._news_coverage = 0.0
        self._social_coverage = 0.0
        self._last_refresh = 0.0
        self._running = False

    def _score_texts(self, texts) -> Tuple[float, float]:
        scored = [
            self.analyzer.polarity_scores(t)["compound"]
            for t in texts
            if t and t.strip()
        ]
        if not scored:
            return 0.0, 0.0
        mean = sum(scored) / len(scored)
        coverage = min(1.0, len(scored) / 20.0)
        return mean, coverage

    async def _refresh(self) -> None:
        if self.news_client:
            try:
                resp = await self.news_client.everything(self.query, page_size=50)
                texts = [
                    (a.get("title") or "") + " " + (a.get("description") or "")
                    for a in resp.get("articles", [])
                ]
                self._news_sent, self._news_coverage = self._score_texts(texts)
            except Exception as e:
                log.warning(f"News fetch failed: {e}")

        if self.x_client:
            try:
                resp = await self.x_client.recent_search(self.query, max_results=50)
                texts = [t.get("text", "") for t in resp.get("data", [])]
                self._social_sent, self._social_coverage = self._score_texts(texts)
            except Exception as e:
                log.warning(f"X fetch failed: {e}")

        self._last_refresh = time.time()

    async def start(self) -> None:
        if not (self.news_client or self.x_client):
            log.info(
                "Sentiment disabled: neither NEWS_API_KEY nor X_BEARER_TOKEN set"
            )
            return
        self._running = True
        while self._running:
            await self._refresh()
            await asyncio.sleep(self.refresh_s)

    async def close(self) -> None:
        self._running = False
        if self.news_client:
            await self.news_client.close()
        if self.x_client:
            await self.x_client.close()

    def current(self) -> dict:
        if self._last_refresh == 0.0:
            return {"news": 0.0, "social": 0.0, "freshness": 0.0, "coverage": 0.0}
        age = time.time() - self._last_refresh
        freshness = max(0.0, 1.0 - age / (self.refresh_s * 2))
        coverage = (self._news_coverage + self._social_coverage) / 2.0
        return {
            "news": self._news_sent,
            "social": self._social_sent,
            "freshness": freshness,
            "coverage": coverage,
        }
