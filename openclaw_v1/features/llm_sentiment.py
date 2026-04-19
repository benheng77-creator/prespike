"""
LLM-backed sentiment tracker.

Drop-in replacement for `features.sentiment.SentimentTracker` — identical
`start()` / `close()` / `current()` interface, but under the hood each
news batch and each X search batch is scored by an LLM (Claude or OpenAI)
instead of VADER's lexicon.

Why bother: VADER has no financial context and can't read crypto-specific
terminology. An LLM will correctly score "SEC approves spot ETF" as a
strong positive and "Exchange suspends withdrawals" as a strong negative,
both of which VADER produces near-neutral scores for.

Additional output over the VADER tracker: `current()["categories"]` is a
count of classification labels from the last news refresh
(regulation / macro / adoption / technical / hack / other). The
`llm_category_risk_boost()` helper maps that distribution to an
`EventRiskScore` boost in [0, 0.5].

Refresh interval defaults to 600 seconds — LLM calls cost money and are
slower than VADER, so you don't want to hammer them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import List, Tuple

from data.llm_providers import ClaudeClient, OpenAIClient
from data.providers import NewsAPIClient, XClient

log = logging.getLogger(__name__)

HIGH_RISK_CATEGORIES = {"hack", "regulation"}


def llm_category_risk_boost(categories: dict) -> float:
    """Map LLM category distribution to an EventRiskScore boost in [0, 0.5]."""
    total = sum(categories.values())
    if total == 0:
        return 0.0
    risky = sum(
        count for cat, count in categories.items() if cat in HIGH_RISK_CATEGORIES
    )
    return min(0.5, 0.5 * risky / total)


def _env_key_for_provider(provider: str) -> str:
    return {"claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}[provider]


class LLMSentimentTracker:
    def __init__(
        self,
        query: str,
        provider: str = "claude",
        model: str = "",
        refresh_s: int = 600,
        max_texts_per_refresh: int = 20,
    ):
        if provider not in ("claude", "openai"):
            raise ValueError(f"provider must be 'claude' or 'openai', got {provider!r}")

        self.query = query
        self.provider = provider
        self.refresh_s = refresh_s
        self.max_texts = max_texts_per_refresh

        self.news_client = NewsAPIClient() if os.getenv("NEWS_API_KEY") else None
        self.x_client = XClient() if os.getenv("X_BEARER_TOKEN") else None

        llm_model = model if model else None
        if provider == "claude":
            self.llm = ClaudeClient(model=llm_model)
        else:
            self.llm = OpenAIClient(model=llm_model)

        self.llm_key_present = bool(os.getenv(_env_key_for_provider(provider)))

        self._news_sent = 0.0
        self._social_sent = 0.0
        self._news_coverage = 0.0
        self._social_coverage = 0.0
        self._categories: dict = {}
        self._last_refresh = 0.0
        self._running = False

    async def _classify_batch(
        self, texts: List[str]
    ) -> Tuple[float, float, dict]:
        """Returns (mean_score, coverage, category_counts)."""
        texts = [t for t in texts if t and t.strip()]
        if not texts or not self.llm_key_present:
            return 0.0, 0.0, {}

        results = await self.llm.score_sentiment_batch(texts[: self.max_texts])
        if not results:
            return 0.0, 0.0, {}

        weighted_sum = 0.0
        total_weight = 0.0
        categories: dict = {}
        for r in results:
            score = float(r.get("score", 0.0))
            conf = float(r.get("confidence", 0.0))
            weighted_sum += score * conf
            total_weight += conf
            cat = r.get("category", "other")
            categories[cat] = categories.get(cat, 0) + 1

        mean_score = weighted_sum / total_weight if total_weight > 0 else 0.0
        coverage = min(1.0, len(results) / 20.0)
        return mean_score, coverage, categories

    async def _refresh(self) -> None:
        if self.news_client:
            try:
                resp = await self.news_client.everything(
                    self.query, page_size=self.max_texts
                )
                texts = [
                    ((a.get("title") or "") + " - " + (a.get("description") or "")).strip()
                    for a in resp.get("articles", [])
                ]
                (
                    self._news_sent,
                    self._news_coverage,
                    self._categories,
                ) = await self._classify_batch(texts)
            except Exception as e:
                log.warning(f"LLM news refresh failed: {e}")

        if self.x_client:
            try:
                resp = await self.x_client.recent_search(
                    self.query, max_results=self.max_texts
                )
                texts = [t.get("text", "") for t in resp.get("data", [])]
                social_score, social_cov, _ = await self._classify_batch(texts)
                self._social_sent = social_score
                self._social_coverage = social_cov
            except Exception as e:
                log.warning(f"LLM X refresh failed: {e}")

        self._last_refresh = time.time()

    async def start(self) -> None:
        if not self.llm_key_present:
            log.info(
                f"LLM sentiment disabled: no API key for provider {self.provider!r}"
            )
            return
        if not (self.news_client or self.x_client):
            log.info("LLM sentiment disabled: no NEWS_API_KEY or X_BEARER_TOKEN")
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
        await self.llm.close()

    def current(self) -> dict:
        if self._last_refresh == 0.0:
            return {
                "news": 0.0,
                "social": 0.0,
                "freshness": 0.0,
                "coverage": 0.0,
                "categories": {},
            }
        age = time.time() - self._last_refresh
        freshness = max(0.0, 1.0 - age / (self.refresh_s * 2))
        coverage = (self._news_coverage + self._social_coverage) / 2.0
        return {
            "news": self._news_sent,
            "social": self._social_sent,
            "freshness": freshness,
            "coverage": coverage,
            "categories": dict(self._categories),
        }
