"""
LLM API clients for structured sentiment / event classification.

Two providers are supported via a compatible interface:

    ClaudeClient  — Anthropic's Messages API (default: claude-haiku-4-5-20251001)
    OpenAIClient  — OpenAI's chat/completions (default: gpt-4o-mini)

Both expose:

    async score_sentiment_batch(texts: list[str]) -> list[dict]

where each dict has `score ∈ [-1, 1]`, `confidence ∈ [0, 1]`, and
`category ∈ {regulation, macro, adoption, technical, hack, other}`.

Keys are read from env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`). If
the key is missing, the client returns neutral stubs rather than raising,
so code paths degrade gracefully when a user runs without a key.

Parsing is tolerant: if the model returns text outside the JSON array, or
an array shorter/longer than the input batch, `_parse_response()` pads or
truncates and falls back to neutral on JSON errors.
"""

from __future__ import annotations

import json
import logging
import os
from typing import List, Optional

import aiohttp

log = logging.getLogger(__name__)

NEUTRAL_RESULT = {"score": 0.0, "confidence": 0.0, "category": "unknown"}

SENTIMENT_PROMPT_TEMPLATE = (
    "You are a crypto/financial news sentiment classifier. For each headline "
    "below, assess sentiment toward BTC and major crypto assets.\n\n"
    "Respond with a single JSON array. One object per headline, in order. "
    "Each object has exactly these keys:\n"
    '  "score":       float in [-1, 1]  (negative=bearish, positive=bullish)\n'
    '  "confidence":  float in [0, 1]   (0=no opinion, 1=very confident)\n'
    '  "category":    one of "regulation", "macro", "adoption", "technical", "hack", "other"\n\n'
    "Output ONLY the JSON array. No prose, no markdown fences.\n\n"
    "Headlines:\n{numbered}"
)


def _build_prompt(texts: List[str]) -> str:
    numbered = "\n".join(f"{i + 1}. {t[:500]}" for i, t in enumerate(texts))
    return SENTIMENT_PROMPT_TEMPLATE.format(numbered=numbered)


def _parse_response(text: str, n: int) -> List[dict]:
    """Tolerant JSON array parser — pads/truncates to n, falls back to neutral."""
    start = text.find("[")
    end = text.rfind("]") + 1
    if start == -1 or end == 0:
        return [dict(NEUTRAL_RESULT) for _ in range(n)]
    try:
        parsed = json.loads(text[start:end])
    except json.JSONDecodeError:
        return [dict(NEUTRAL_RESULT) for _ in range(n)]
    if not isinstance(parsed, list):
        return [dict(NEUTRAL_RESULT) for _ in range(n)]
    normalized: List[dict] = []
    for item in parsed[:n]:
        if not isinstance(item, dict):
            normalized.append(dict(NEUTRAL_RESULT))
            continue
        score = item.get("score", 0.0)
        conf = item.get("confidence", 0.0)
        category = item.get("category", "other")
        try:
            score = max(-1.0, min(1.0, float(score)))
            conf = max(0.0, min(1.0, float(conf)))
        except (TypeError, ValueError):
            score, conf = 0.0, 0.0
        normalized.append(
            {"score": score, "confidence": conf, "category": str(category)}
        )
    while len(normalized) < n:
        normalized.append(dict(NEUTRAL_RESULT))
    return normalized


class _BaseLLMClient:
    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class ClaudeClient(_BaseLLMClient):
    DEFAULT_MODEL = "claude-haiku-4-5-20251001"
    BASE_URL = "https://api.anthropic.com/v1"

    def __init__(self, model: Optional[str] = None) -> None:
        super().__init__()
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.model = model or self.DEFAULT_MODEL

    async def score_sentiment_batch(self, texts: List[str]) -> List[dict]:
        if not texts:
            return []
        if not self.api_key:
            return [dict(NEUTRAL_RESULT) for _ in texts]

        prompt = _build_prompt(texts)
        session = await self._get_session()
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": self.model,
            "max_tokens": 4000,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            async with session.post(
                f"{self.BASE_URL}/messages", headers=headers, json=body
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except Exception as e:
            log.warning(f"Claude API call failed: {e}")
            return [dict(NEUTRAL_RESULT) for _ in texts]

        try:
            text = data["content"][0]["text"]
        except (KeyError, IndexError, TypeError):
            log.warning("Claude response missing content[0].text")
            return [dict(NEUTRAL_RESULT) for _ in texts]

        return _parse_response(text, len(texts))


class OpenAIClient(_BaseLLMClient):
    DEFAULT_MODEL = "gpt-4o-mini"
    BASE_URL = "https://api.openai.com/v1"

    def __init__(self, model: Optional[str] = None) -> None:
        super().__init__()
        self.api_key = os.getenv("OPENAI_API_KEY", "")
        self.model = model or self.DEFAULT_MODEL

    async def score_sentiment_batch(self, texts: List[str]) -> List[dict]:
        if not texts:
            return []
        if not self.api_key:
            return [dict(NEUTRAL_RESULT) for _ in texts]

        # Ask OpenAI to wrap the array in an object with a known key, since
        # json_object mode requires a top-level object.
        prompt = _build_prompt(texts) + (
            '\n\nReturn a JSON object of the form {"results": [...]} '
            "where results is the array described above."
        )
        session = await self._get_session()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "max_tokens": 2000,
        }
        try:
            async with session.post(
                f"{self.BASE_URL}/chat/completions", headers=headers, json=body
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except Exception as e:
            log.warning(f"OpenAI API call failed: {e}")
            return [dict(NEUTRAL_RESULT) for _ in texts]

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            log.warning("OpenAI response missing choices[0].message.content")
            return [dict(NEUTRAL_RESULT) for _ in texts]

        # Wrap object unwrap: {"results": [...]}
        try:
            parsed_obj = json.loads(text)
            if isinstance(parsed_obj, dict) and isinstance(
                parsed_obj.get("results"), list
            ):
                arr_text = json.dumps(parsed_obj["results"])
                return _parse_response(arr_text, len(texts))
        except json.JSONDecodeError:
            pass

        return _parse_response(text, len(texts))
