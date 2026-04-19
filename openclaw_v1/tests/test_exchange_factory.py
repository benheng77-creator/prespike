import pytest

from core.exchange_factory import (
    EXCHANGES,
    active_exchange_id,
    available,
    credentials_for,
    is_configured,
    summary,
)


def test_every_new_exchange_registered():
    for eid in ("coinbase", "okx", "cryptocom", "independentreserve"):
        assert eid in EXCHANGES
    assert set(available()) >= {"binance", "coinbase", "okx", "cryptocom", "independentreserve"}


def test_is_configured_false_without_keys(monkeypatch):
    for eid in available():
        for k in ("API_KEY", "API_SECRET",
                  "COINBASE_API_KEY", "COINBASE_API_SECRET", "COINBASE_PASSPHRASE",
                  "OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE",
                  "CRYPTOCOM_API_KEY", "CRYPTOCOM_API_SECRET",
                  "INDEPENDENTRESERVE_API_KEY", "INDEPENDENTRESERVE_API_SECRET"):
            monkeypatch.delenv(k, raising=False)
        assert is_configured(eid) is False


def test_is_configured_true_when_keys_present(monkeypatch):
    monkeypatch.setenv("COINBASE_API_KEY", "cb-key")
    monkeypatch.setenv("COINBASE_API_SECRET", "cb-secret")
    # no passphrase on Advanced Trade (legacy Pro only) — new flow doesn't require it
    # to reflect reality we require it here because our meta flag is passphrase=True.
    monkeypatch.setenv("COINBASE_PASSPHRASE", "cb-pass")
    assert is_configured("coinbase") is True


def test_okx_requires_passphrase(monkeypatch):
    monkeypatch.setenv("OKX_API_KEY", "k")
    monkeypatch.setenv("OKX_API_SECRET", "s")
    monkeypatch.delenv("OKX_PASSPHRASE", raising=False)
    assert is_configured("okx") is False
    monkeypatch.setenv("OKX_PASSPHRASE", "p")
    assert is_configured("okx") is True


def test_cryptocom_and_ir_no_passphrase(monkeypatch):
    monkeypatch.setenv("CRYPTOCOM_API_KEY", "k")
    monkeypatch.setenv("CRYPTOCOM_API_SECRET", "s")
    assert is_configured("cryptocom") is True

    monkeypatch.setenv("INDEPENDENTRESERVE_API_KEY", "k")
    monkeypatch.setenv("INDEPENDENTRESERVE_API_SECRET", "s")
    assert is_configured("independentreserve") is True


def test_active_default_is_binance(monkeypatch):
    monkeypatch.delenv("ACTIVE_EXCHANGE", raising=False)
    assert active_exchange_id() == "binance"


def test_summary_shape(monkeypatch):
    monkeypatch.setenv("ACTIVE_EXCHANGE", "coinbase")
    s = summary()
    assert s["active"] == "coinbase"
    assert set(s["configured"].keys()) == set(available())
    assert "coinbase" in s["supported"]


def test_build_client_errors_when_unconfigured(monkeypatch):
    pytest.importorskip("ccxt")
    from core.exchange_factory import build_client
    for k in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError):
        build_client("okx")


def test_secrets_catalog_lists_new_keys():
    from secrets_store import CATALOG
    names = {k.name for k in CATALOG}
    for n in (
        "COINBASE_API_KEY", "COINBASE_API_SECRET", "COINBASE_PASSPHRASE",
        "OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE",
        "CRYPTOCOM_API_KEY", "CRYPTOCOM_API_SECRET",
        "INDEPENDENTRESERVE_API_KEY", "INDEPENDENTRESERVE_API_SECRET",
        "ACTIVE_EXCHANGE",
        "CLAUDE_API_KEY", "CODEX_API_KEY",
    ):
        assert n in names, f"{n} missing from CATALOG"
