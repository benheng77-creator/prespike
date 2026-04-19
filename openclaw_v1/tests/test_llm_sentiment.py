"""Unit tests for LLM sentiment clients and the LLMSentimentTracker."""

import asyncio
import json
import os

import pytest

from data.llm_providers import (
    NEUTRAL_RESULT,
    ClaudeClient,
    OpenAIClient,
    _build_prompt,
    _parse_response,
)
from features.llm_sentiment import (
    HIGH_RISK_CATEGORIES,
    LLMSentimentTracker,
    llm_category_risk_boost,
)


# ---------- prompt + parser ----------


def test_build_prompt_numbers_headlines_in_order():
    prompt = _build_prompt(["first headline", "second headline"])
    assert "1. first headline" in prompt
    assert "2. second headline" in prompt
    assert "JSON array" in prompt


def test_parse_valid_response():
    raw = """Some preamble [{"score": 0.8, "confidence": 0.9, "category": "adoption"},
    {"score": -0.5, "confidence": 0.6, "category": "regulation"}] and trailing noise"""
    out = _parse_response(raw, 2)
    assert len(out) == 2
    assert out[0]["score"] == 0.8
    assert out[0]["category"] == "adoption"
    assert out[1]["score"] == -0.5
    assert out[1]["category"] == "regulation"


def test_parse_clips_out_of_range_scores():
    raw = '[{"score": 5.0, "confidence": 2.0, "category": "macro"}]'
    out = _parse_response(raw, 1)
    assert out[0]["score"] == 1.0
    assert out[0]["confidence"] == 1.0


def test_parse_pads_when_llm_returns_fewer():
    raw = '[{"score": 0.5, "confidence": 0.7, "category": "macro"}]'
    out = _parse_response(raw, 3)
    assert len(out) == 3
    assert out[0]["score"] == 0.5
    assert out[1] == NEUTRAL_RESULT
    assert out[2] == NEUTRAL_RESULT


def test_parse_truncates_when_llm_returns_more():
    raw = json.dumps(
        [{"score": i / 10, "confidence": 0.5, "category": "other"} for i in range(10)]
    )
    out = _parse_response(raw, 3)
    assert len(out) == 3


def test_parse_malformed_json_returns_neutrals():
    raw = "[this is not json at all]"
    out = _parse_response(raw, 2)
    assert out == [NEUTRAL_RESULT, NEUTRAL_RESULT]


def test_parse_no_array_returns_neutrals():
    raw = "The model decided not to output JSON today"
    out = _parse_response(raw, 2)
    assert out == [NEUTRAL_RESULT, NEUTRAL_RESULT]


def test_parse_dict_items_fall_back_gracefully():
    raw = '[{"score": "not a number", "category": "other"}]'
    out = _parse_response(raw, 1)
    assert out[0]["score"] == 0.0  # fell back
    assert out[0]["category"] == "other"


# ---------- client fallbacks (no network) ----------


def _without_key(env_var: str):
    saved = os.environ.pop(env_var, None)
    return saved


def _restore_key(env_var: str, saved):
    if saved is not None:
        os.environ[env_var] = saved


def test_claude_no_api_key_returns_neutrals():
    saved = _without_key("ANTHROPIC_API_KEY")
    try:
        client = ClaudeClient()
        result = asyncio.run(client.score_sentiment_batch(["a", "b", "c"]))
        assert result == [NEUTRAL_RESULT, NEUTRAL_RESULT, NEUTRAL_RESULT]
        asyncio.run(client.close())
    finally:
        _restore_key("ANTHROPIC_API_KEY", saved)


def test_claude_empty_batch_returns_empty_list():
    client = ClaudeClient()
    assert asyncio.run(client.score_sentiment_batch([])) == []
    asyncio.run(client.close())


def test_openai_no_api_key_returns_neutrals():
    saved = _without_key("OPENAI_API_KEY")
    try:
        client = OpenAIClient()
        result = asyncio.run(client.score_sentiment_batch(["a", "b"]))
        assert result == [NEUTRAL_RESULT, NEUTRAL_RESULT]
        asyncio.run(client.close())
    finally:
        _restore_key("OPENAI_API_KEY", saved)


def test_openai_uses_custom_model():
    client = OpenAIClient(model="gpt-5-turbo-future")
    assert client.model == "gpt-5-turbo-future"
    asyncio.run(client.close())


def test_claude_uses_default_model_when_none_given():
    client = ClaudeClient()
    assert client.model == ClaudeClient.DEFAULT_MODEL
    asyncio.run(client.close())


# ---------- category risk boost helper ----------


def test_risk_boost_empty_returns_zero():
    assert llm_category_risk_boost({}) == 0.0


def test_risk_boost_only_safe_categories():
    assert llm_category_risk_boost({"adoption": 5, "macro": 3}) == 0.0


def test_risk_boost_all_risky_saturates_at_half():
    assert llm_category_risk_boost({"hack": 4, "regulation": 6}) == 0.5


def test_risk_boost_mixed():
    # 1 risky out of 4 total → 0.5 * 0.25 = 0.125
    boost = llm_category_risk_boost(
        {"hack": 1, "adoption": 2, "macro": 1}
    )
    assert abs(boost - 0.125) < 1e-9


def test_high_risk_categories_set_is_stable():
    assert "hack" in HIGH_RISK_CATEGORIES
    assert "regulation" in HIGH_RISK_CATEGORIES


# ---------- LLMSentimentTracker with a fake LLM ----------


class _FakeLLMClient:
    def __init__(self, score=0.5, confidence=0.8, category="adoption"):
        self.score = score
        self.confidence = confidence
        self.category = category
        self.calls = 0

    async def score_sentiment_batch(self, texts):
        self.calls += 1
        return [
            {
                "score": self.score,
                "confidence": self.confidence,
                "category": self.category,
            }
            for _ in texts
        ]

    async def close(self):
        pass


def _tracker_with_fake_llm(score=0.5, confidence=0.8, category="adoption"):
    t = LLMSentimentTracker(query="BTC", provider="claude")
    t.llm = _FakeLLMClient(score=score, confidence=confidence, category=category)
    t.llm_key_present = True
    return t


def test_tracker_classify_batch_averages_weighted_by_confidence():
    t = _tracker_with_fake_llm(score=0.5, confidence=0.8)
    mean, cov, cats = asyncio.run(t._classify_batch(["a", "b", "c"]))
    assert abs(mean - 0.5) < 1e-9
    assert cov > 0
    assert cats.get("adoption") == 3


def test_tracker_classify_batch_empty_input_returns_zero():
    t = _tracker_with_fake_llm()
    mean, cov, cats = asyncio.run(t._classify_batch([]))
    assert (mean, cov, cats) == (0.0, 0.0, {})


def test_tracker_classify_batch_no_key_returns_zero():
    t = LLMSentimentTracker(query="BTC", provider="claude")
    t.llm = _FakeLLMClient()
    t.llm_key_present = False
    mean, cov, cats = asyncio.run(t._classify_batch(["real text"]))
    assert (mean, cov, cats) == (0.0, 0.0, {})


def test_tracker_current_before_refresh_is_neutral():
    t = _tracker_with_fake_llm()
    snap = t.current()
    assert snap["news"] == 0.0
    assert snap["social"] == 0.0
    assert snap["freshness"] == 0.0
    assert snap["categories"] == {}


def test_tracker_invalid_provider_raises():
    with pytest.raises(ValueError, match="provider must be"):
        LLMSentimentTracker(query="BTC", provider="llama")
