"""Opportunity Fabric — Sprint 2 tests: Provenance fingerprints."""
from __future__ import annotations

import importlib
import json
import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture
def _iso_prov(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    monkeypatch.setenv("SIGN_FLIP_COMMIT", "phase-vv-abc123")
    import spot_aggro.governance.provenance as prov
    importlib.reload(prov)
    # Create the parent table the module alters.
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE IF NOT EXISTS spot_live_variant_entries("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " variant TEXT, symbol TEXT, status TEXT,"
        " notional_usd REAL, realized_pnl_usd REAL,"
        " opened_ts_ms INTEGER, closed_ts_ms INTEGER)"
    )
    con.commit()
    con.close()
    yield db, prov


def _insert_entry(db: Path, variant: str, symbol: str) -> int:
    con = sqlite3.connect(str(db))
    cur = con.execute(
        "INSERT INTO spot_live_variant_entries("
        " variant, symbol, status, notional_usd, opened_ts_ms)"
        " VALUES(?,?,?,?,?)",
        (variant, symbol, "open", 25.0, int(time.time() * 1000)),
    )
    entry_id = cur.lastrowid
    con.commit()
    con.close()
    return entry_id


def test_build_captures_global_context(_iso_prov):
    _, prov = _iso_prov
    p = prov.build(
        variant="contrarian",
        symbol="BTC-USDT",
        tier="C",
        scorer_version="v12",
        score_value=0.72,
        notional_usd=25.0,
    )
    assert p.variant == "contrarian"
    assert p.symbol == "BTC-USDT"
    assert p.schema_version == prov.PROVENANCE_SCHEMA_VERSION
    assert p.sign_flip_commit == "phase-vv-abc123"
    # server_build is optional but field must exist.
    assert hasattr(p, "server_build")


def test_fingerprint_deterministic(_iso_prov):
    _, prov = _iso_prov
    args = dict(variant="deep_value", symbol="ETH-USDT",
                scorer_version="v12", score_value=0.5)
    p1 = prov.build(**args)
    p2 = prov.build(**args)
    # ts_ms may differ by a millisecond, so pin it.
    p2.ts_ms = p1.ts_ms
    assert p1.fingerprint() == p2.fingerprint()


def test_fingerprint_changes_with_any_field(_iso_prov):
    _, prov = _iso_prov
    p = prov.build(variant="contrarian", symbol="BTC-USDT", score_value=0.5)
    baseline = p.fingerprint()
    p.score_value = 0.6
    assert p.fingerprint() != baseline


def test_attach_and_fetch_roundtrip(_iso_prov):
    db, prov = _iso_prov
    entry_id = _insert_entry(db, "contrarian", "BTC-USDT")
    p = prov.build(variant="contrarian", symbol="BTC-USDT",
                   score_value=0.7, score_components={"ret_7d": -0.08})
    assert prov.attach_to_entry(entry_id, p) is True
    fetched = prov.fetch(entry_id)
    assert fetched is not None
    assert fetched.variant == "contrarian"
    assert fetched.score_value == 0.7
    assert fetched.score_components == {"ret_7d": -0.08}


def test_verify_detects_tampering(_iso_prov):
    db, prov = _iso_prov
    entry_id = _insert_entry(db, "contrarian", "BTC-USDT")
    p = prov.build(variant="contrarian", symbol="BTC-USDT", score_value=0.7)
    prov.attach_to_entry(entry_id, p)
    # Manually corrupt the stored JSON.
    con = sqlite3.connect(str(db))
    row = con.execute(
        "SELECT provenance_json FROM spot_live_variant_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    tampered = json.loads(row[0])
    tampered["score_value"] = 99.9       # mutate after fingerprint was computed
    con.execute(
        "UPDATE spot_live_variant_entries SET provenance_json = ? WHERE id = ?",
        (json.dumps(tampered), entry_id),
    )
    con.commit()
    con.close()
    result = prov.verify(entry_id)
    assert result["ok"] is False
    assert result["stored_fingerprint"] != result["recomputed_fingerprint"]


def test_verify_passes_on_clean_record(_iso_prov):
    db, prov = _iso_prov
    entry_id = _insert_entry(db, "contrarian", "BTC-USDT")
    p = prov.build(variant="contrarian", symbol="BTC-USDT", score_value=0.5)
    prov.attach_to_entry(entry_id, p)
    result = prov.verify(entry_id)
    assert result["ok"] is True


def test_fetch_raw_includes_trade_context(_iso_prov):
    db, prov = _iso_prov
    entry_id = _insert_entry(db, "deep_value", "SOL-USDT")
    p = prov.build(variant="deep_value", symbol="SOL-USDT", score_value=0.6)
    prov.attach_to_entry(entry_id, p)
    raw = prov.fetch_raw(entry_id)
    assert raw is not None
    assert raw["entry_id"] == entry_id
    assert raw["variant"] == "deep_value"
    assert raw["symbol"] == "SOL-USDT"
    assert raw["provenance"] is not None
    assert "_fingerprint" in raw["provenance"]


def test_ensure_column_is_idempotent(_iso_prov):
    _, prov = _iso_prov
    prov._ensure_provenance_column()
    prov._ensure_provenance_column()   # second call must not raise
    prov._ensure_provenance_column()


def test_fetch_returns_none_when_no_provenance_attached(_iso_prov):
    db, prov = _iso_prov
    entry_id = _insert_entry(db, "contrarian", "BTC-USDT")
    assert prov.fetch(entry_id) is None
    assert prov.fetch_raw(entry_id)["provenance"] is None


def test_build_survives_missing_mio(_iso_prov, monkeypatch):
    _, prov = _iso_prov
    # Simulate MIO unavailable — build() must not raise.
    def _raise():
        raise RuntimeError("MIO offline")
    monkeypatch.setattr(prov, "_current_mio", lambda: {})
    p = prov.build(variant="contrarian", symbol="BTC-USDT")
    assert p.regime is None
    assert p.universe_snapshot_hash is None
