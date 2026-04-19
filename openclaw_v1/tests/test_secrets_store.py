from pathlib import Path

import pytest

from audit import AuditLedger
from secrets_store import SecretsStore, mask_value


@pytest.fixture()
def store(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# existing\nTRADE_DB_PATH=/tmp/x.db\nOPENAI_API_KEY=sk-abcdefghijklmnop\n", encoding="utf-8")
    ledger = AuditLedger(db_path=str(tmp_path / "a.db"), jsonl_path=str(tmp_path / "a.jsonl"))
    return SecretsStore(env_file=str(env), ledger=ledger), ledger, env


def test_mask_short_value():
    assert mask_value("abcd") == "••••"


def test_mask_medium_value():
    m = mask_value("sk-abcdefghij")  # 13 chars
    assert m and m.startswith("sk-a") and m.endswith("hij") and "•" in m


def test_mask_none_and_empty():
    assert mask_value(None) is None
    assert mask_value("") is None


def test_non_sensitive_not_masked():
    assert mask_value("paper", sensitive=False) == "paper"


def test_list_shows_catalog_with_set_flag(store, monkeypatch):
    # Defensive: some dev boxes (and some test-suite orderings) leave
    # ANTHROPIC_API_KEY set in the process env. The SecretsStore falls
    # back to os.environ for keys not in the .env, so clear it for this
    # assertion to be deterministic.
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "GEMINI_API_KEY", "CODEX_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    s, _, _ = store
    rows = s.list()
    by_name = {r["name"]: r for r in rows}
    assert by_name["OPENAI_API_KEY"]["set"] is True
    assert by_name["OPENAI_API_KEY"]["masked"] != "sk-abcdefghijklmnop"
    assert by_name["TRADE_DB_PATH"]["set"] is True
    assert by_name["ANTHROPIC_API_KEY"]["set"] is False


def test_set_updates_existing(store):
    s, _, env = store
    s.set("OPENAI_API_KEY", "sk-NEW-value")
    assert "sk-NEW-value" in env.read_text(encoding="utf-8")


def test_set_appends_new(store):
    s, _, env = store
    s.set("TELEGRAM_BOT_TOKEN", "123:abc")
    assert "TELEGRAM_BOT_TOKEN=123:abc" in env.read_text(encoding="utf-8")


def test_set_quotes_values_with_spaces(store):
    s, _, env = store
    s.set("REDDIT_USER_AGENT", "openclaw-bot 0.1")
    body = env.read_text(encoding="utf-8")
    assert '"openclaw-bot 0.1"' in body


def test_delete_removes_line(store):
    s, _, env = store
    assert s.delete("OPENAI_API_KEY") is True
    assert "OPENAI_API_KEY" not in env.read_text(encoding="utf-8")


def test_delete_missing_returns_false(store):
    s, _, _ = store
    assert s.delete("NOT_A_KEY") is False


def test_audit_row_on_set(store):
    s, ledger, _ = store
    s.set("NEWS_API_KEY", "abc123")
    rows = ledger.fetch_recent(5)
    assert any(r["kind"] == "secret" and r["phase"] == "secret_set" for r in rows)


def test_rejects_bad_key_name(store):
    s, _, _ = store
    with pytest.raises(ValueError):
        s.set("bad key", "value")


def test_get_raw_returns_unmasked(store):
    s, _, _ = store
    assert s.get_raw("OPENAI_API_KEY") == "sk-abcdefghijklmnop"


def test_catalog_contains_expected_keys():
    from secrets_store import catalog_by_group
    grouped = catalog_by_group()
    assert "Exchange" in grouped
    assert "LLM" in grouped
    assert any(k.name == "API_KEY" for k in grouped["Exchange"])
    assert any(k.name == "OPENAI_API_KEY" for k in grouped["LLM"])
