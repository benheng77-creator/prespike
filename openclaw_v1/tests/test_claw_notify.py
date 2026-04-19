"""
Claw notify tests — channel resolution, fan-out, dedupe, graceful failure.

No real network: _http_post / _http_get are monkey-patched per test.
"""

from __future__ import annotations

import os
import sys

import pytest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from claw import notify  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Clear env + dedupe between tests."""
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
              "CALLMEBOT_API_KEY", "CALLMEBOT_PHONE",
              "CLAW_NOTIFY_CHANNELS"):
        monkeypatch.delenv(k, raising=False)
    notify._reset_dedupe_for_tests()


# ---------------------------------------------------------------------------
# Channel detection
# ---------------------------------------------------------------------------

def test_no_channels_when_env_missing():
    assert notify.configured_channels() == []


def test_telegram_detected_when_both_vars_set(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    assert "telegram" in notify.configured_channels()


def test_whatsapp_detected_when_both_vars_set(monkeypatch):
    monkeypatch.setenv("CALLMEBOT_API_KEY", "k")
    monkeypatch.setenv("CALLMEBOT_PHONE", "+6500000000")
    assert "whatsapp" in notify.configured_channels()


def test_telegram_missing_chat_id_not_configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    # no TELEGRAM_CHAT_ID
    assert "telegram" not in notify.configured_channels()


# ---------------------------------------------------------------------------
# send_telegram
# ---------------------------------------------------------------------------

def test_send_telegram_not_configured_returns_structured_fail():
    r = notify.send_telegram("hi")
    assert r.ok is False
    assert r.detail == "not_configured"


def test_send_telegram_success(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    calls = {}
    def fake_post(url, payload, timeout=8.0):
        calls["url"] = url; calls["payload"] = payload
        return 200, '{"ok":true,"result":{}}'
    monkeypatch.setattr(notify, "_http_post", fake_post)
    r = notify.send_telegram("hello", severity="warn", tag="tests")
    assert r.ok is True
    assert "tests" in calls["payload"]["text"]
    assert "hello" in calls["payload"]["text"]


def test_send_telegram_failure_returns_structured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(notify, "_http_post", lambda *a, **k: (401, "unauth"))
    r = notify.send_telegram("x")
    assert r.ok is False
    assert "401" in r.detail


# ---------------------------------------------------------------------------
# send_whatsapp
# ---------------------------------------------------------------------------

def test_send_whatsapp_not_configured():
    r = notify.send_whatsapp("hi")
    assert r.ok is False
    assert r.detail == "not_configured"


def test_send_whatsapp_success(monkeypatch):
    monkeypatch.setenv("CALLMEBOT_API_KEY", "k")
    monkeypatch.setenv("CALLMEBOT_PHONE", "+6500000000")
    monkeypatch.setattr(notify, "_http_get",
                        lambda url, timeout=10.0: (200, "Message queued ok"))
    r = notify.send_whatsapp("hello")
    assert r.ok is True


def test_send_whatsapp_rejected(monkeypatch):
    monkeypatch.setenv("CALLMEBOT_API_KEY", "k")
    monkeypatch.setenv("CALLMEBOT_PHONE", "+6500000000")
    monkeypatch.setattr(notify, "_http_get",
                        lambda url, timeout=10.0: (200, "You are not registered"))
    r = notify.send_whatsapp("x")
    assert r.ok is False


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------

def test_notify_fans_out_to_every_configured_channel(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("CALLMEBOT_API_KEY", "k"); monkeypatch.setenv("CALLMEBOT_PHONE", "+60000")
    monkeypatch.setattr(notify, "_http_post", lambda *a, **k: (200, '{"ok":true}'))
    monkeypatch.setattr(notify, "_http_get", lambda *a, **k: (200, "Message sent"))
    results = notify.notify("hello world")
    chans = {r.channel for r in results}
    assert chans == {"telegram", "whatsapp"}
    assert all(r.ok for r in results)


def test_notify_never_raises_on_network_error(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    def boom(*a, **k): raise RuntimeError("network dead")
    monkeypatch.setattr(notify, "_http_post", boom)
    results = notify.notify("x")
    assert len(results) == 1
    assert results[0].ok is False


def test_notify_respects_explicit_channel_filter(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("CALLMEBOT_API_KEY", "k"); monkeypatch.setenv("CALLMEBOT_PHONE", "+60000")
    monkeypatch.setattr(notify, "_http_post", lambda *a, **k: (200, '{"ok":true}'))
    results = notify.notify("x", channels=["telegram"])
    assert [r.channel for r in results] == ["telegram"]


def test_notify_respects_env_pin(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("CALLMEBOT_API_KEY", "k"); monkeypatch.setenv("CALLMEBOT_PHONE", "+60000")
    monkeypatch.setenv("CLAW_NOTIFY_CHANNELS", "whatsapp")
    monkeypatch.setattr(notify, "_http_get", lambda *a, **k: (200, "Message sent"))
    results = notify.notify("x")
    assert [r.channel for r in results] == ["whatsapp"]


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def test_duplicate_message_is_suppressed_within_window(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    calls = {"n": 0}
    def fake_post(*a, **k): calls["n"] += 1; return 200, '{"ok":true}'
    monkeypatch.setattr(notify, "_http_post", fake_post)
    a = notify.send_telegram("same text")
    b = notify.send_telegram("same text")
    assert a.ok and b.ok
    assert calls["n"] == 1    # second call deduped
    assert b.detail == "deduped"


def test_different_messages_are_not_deduped(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t"); monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    calls = {"n": 0}
    def fake_post(*a, **k): calls["n"] += 1; return 200, '{"ok":true}'
    monkeypatch.setattr(notify, "_http_post", fake_post)
    notify.send_telegram("a")
    notify.send_telegram("b")
    assert calls["n"] == 2
