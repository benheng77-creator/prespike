"""
Claw LLM tests — provider availability, dispatch, auto selection, failure modes.

No real network: _post is monkey-patched.
"""

from __future__ import annotations

import json
import os
import sys

import pytest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from claw import llm  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
              "MISTRAL_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# available_providers + _pick_provider
# ---------------------------------------------------------------------------

def test_available_providers_empty():
    assert llm.available_providers() == []


def test_available_providers_each_detected(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("MISTRAL_API_KEY", "x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    got = set(llm.available_providers())
    assert got == {"openai", "anthropic", "gemini", "mistral", "openrouter"}


def test_pick_provider_honours_explicit_when_available(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "x")
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert llm._pick_provider("mistral") == "mistral"


def test_pick_provider_auto_uses_preference_order(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    # preference: gemini > mistral > openai > anthropic > openrouter
    assert llm._pick_provider("auto") == "gemini"


def test_pick_provider_returns_empty_when_nothing_available():
    assert llm._pick_provider("auto") == ""


# ---------------------------------------------------------------------------
# complete() — success paths
# ---------------------------------------------------------------------------

def test_complete_no_provider(monkeypatch):
    out = llm.complete("hi")
    assert out["ok"] is False
    assert out["reason"] == "no_provider_available"


def test_complete_empty_prompt(monkeypatch):
    out = llm.complete("")
    assert out["ok"] is False


def test_complete_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    def fake_post(url, body, headers, timeout=20.0):
        assert "openai.com" in url
        assert headers["Authorization"].startswith("Bearer ")
        return 200, json.dumps({
            "choices": [{"message": {"content": "hello"}}]
        })
    monkeypatch.setattr(llm, "_post", fake_post)
    out = llm.complete("hi", provider="openai")
    assert out["ok"] is True
    assert out["text"] == "hello"
    assert out["provider"] == "openai"
    assert out["authoritative"] is False


def test_complete_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    def fake_post(url, body, headers, timeout=20.0):
        assert "anthropic.com" in url
        assert headers["anthropic-version"] == "2023-06-01"
        return 200, json.dumps({
            "content": [{"type": "text", "text": "hi back"}]
        })
    monkeypatch.setattr(llm, "_post", fake_post)
    out = llm.complete("hi", provider="anthropic")
    assert out["ok"] is True
    assert out["text"] == "hi back"


def test_complete_gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    def fake_post(url, body, headers, timeout=20.0):
        assert "generativelanguage.googleapis.com" in url
        return 200, json.dumps({
            "candidates": [{"content": {"parts": [{"text": "sure"}]}}]
        })
    monkeypatch.setattr(llm, "_post", fake_post)
    out = llm.complete("hi", provider="gemini")
    assert out["ok"] is True
    assert out["text"] == "sure"


def test_complete_mistral(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    def fake_post(url, body, headers, timeout=20.0):
        assert "mistral.ai" in url
        return 200, json.dumps({
            "choices": [{"message": {"content": "mistral ok"}}]
        })
    monkeypatch.setattr(llm, "_post", fake_post)
    out = llm.complete("hi", provider="mistral")
    assert out["ok"] is True
    assert out["text"] == "mistral ok"


def test_complete_openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    def fake_post(url, body, headers, timeout=20.0):
        assert "openrouter.ai" in url
        return 200, json.dumps({
            "choices": [{"message": {"content": "routed"}}]
        })
    monkeypatch.setattr(llm, "_post", fake_post)
    out = llm.complete("hi", provider="openrouter")
    assert out["ok"] is True
    assert out["text"] == "routed"


# ---------------------------------------------------------------------------
# complete() — failure paths never raise
# ---------------------------------------------------------------------------

def test_complete_http_error_is_structured(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(llm, "_post", lambda *a, **k: (500, "server err"))
    out = llm.complete("hi")
    assert out["ok"] is False
    assert "500" in out["reason"]


def test_complete_parse_error_is_structured(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(llm, "_post", lambda *a, **k: (200, "not json"))
    out = llm.complete("hi")
    assert out["ok"] is False


def test_complete_always_tags_non_authoritative(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(llm, "_post", lambda *a, **k: (200, json.dumps({
        "candidates": [{"content": {"parts": [{"text": "hi"}]}}]
    })))
    out = llm.complete("hi")
    assert out["source"] == "claw.llm"
    assert out["authoritative"] is False
