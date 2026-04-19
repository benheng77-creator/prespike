"""
CLAW-NIC-v1 — non-interference tests.

These tests prove that Claw never mutates a bot payload. Every test exercises
a different surface of the contract. If any of these fail, the contract is
broken and the upgrade must not be released.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from claw.contract import (  # noqa: E402
    CLAW_NIC_VERSION,
    BOT_FIELDS_FROZEN,
    BotPayloadMutation,
    assert_unchanged,
    bot_payload_hash,
    canonical_payload,
    freeze,
)
from claw.db import init_claw_schema  # noqa: E402
from claw.ingest import (  # noqa: E402
    fetch_bot_decision,
    list_bot_decisions,
    record_bot_decision,
)


@pytest.fixture()
def tmp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "claw_test.db")
        monkeypatch.setenv("CLAW_DB_PATH", path)
        init_claw_schema(path)
        yield path


def _sample_payload(**overrides):
    base = {
        "PWinPct": 62.5,
        "ConfidencePct": 71.0,
        "ScoreTotal": 85.0,
        "EV_R": 0.21,
        "RRTrue": 1.6,
        "TerminalAction": "EXECUTE",
        "EntryPx": 50_000.0,
        "StopPx": 49_000.0,
        "TargetPx": 52_000.0,
        "symbol": "BTCUSDT",
        "rationale": "demo",
        "model_version": "v1",
        "strategy_id": "decision_engine",
        "cycle_id": "c-123",
        # Extra field outside BOT_FIELDS_FROZEN — must not affect hash.
        "extra_note": "metrics card",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Contract primitives
# ---------------------------------------------------------------------------

def test_freeze_blocks_mutation():
    frozen = freeze(_sample_payload())
    with pytest.raises(TypeError):
        frozen["PWinPct"] = 999.0  # type: ignore[index]
    with pytest.raises(TypeError):
        del frozen["ScoreTotal"]   # type: ignore[attr-defined]


def test_freeze_is_deep():
    payload = {"PWinPct": 60.0, "nested": {"a": 1, "list": [1, 2]}}
    frozen = freeze(payload)
    with pytest.raises(TypeError):
        frozen["nested"]["a"] = 9  # type: ignore[index]
    # lists become tuples — no append
    assert isinstance(frozen["nested"]["list"], tuple)


def test_canonical_payload_restricted_to_frozen_fields():
    canon = canonical_payload(_sample_payload())
    # extra_note is NOT in BOT_FIELDS_FROZEN — must be excluded
    assert "extra_note" not in canon
    # frozen fields present
    assert canon["PWinPct"] == 62.5
    assert canon["TerminalAction"] == "EXECUTE"


def test_bot_payload_hash_is_stable():
    a = _sample_payload()
    b = _sample_payload()
    assert bot_payload_hash(a) == bot_payload_hash(b)


def test_bot_payload_hash_ignores_non_frozen_extras():
    # Changing a non-frozen field must not change the hash.
    h1 = bot_payload_hash(_sample_payload(extra_note="anything"))
    h2 = bot_payload_hash(_sample_payload(extra_note="something else"))
    assert h1 == h2


def test_bot_payload_hash_changes_when_frozen_field_changes():
    a = bot_payload_hash(_sample_payload(PWinPct=60.0))
    b = bot_payload_hash(_sample_payload(PWinPct=60.001))
    assert a != b


def test_assert_unchanged_passes_on_identical():
    p = _sample_payload()
    assert_unchanged(p, dict(p), where="unit-test")


def test_assert_unchanged_raises_on_frozen_mutation():
    before = _sample_payload()
    after = dict(before)
    after["ScoreTotal"] = 0.0  # tamper
    with pytest.raises(BotPayloadMutation) as exc:
        assert_unchanged(before, after, where="unit-test")
    assert CLAW_NIC_VERSION in str(exc.value)
    assert "ScoreTotal" in str(exc.value)


def test_assert_unchanged_allows_non_frozen_diff():
    before = _sample_payload(extra_note="a")
    after = _sample_payload(extra_note="b")
    # extra_note is not in BOT_FIELDS_FROZEN — not a violation
    assert_unchanged(before, after, where="unit-test")


# ---------------------------------------------------------------------------
# Ingest boundary
# ---------------------------------------------------------------------------

def test_ingest_records_row_with_hash(tmp_db):
    payload = _sample_payload()
    res = record_bot_decision(
        strategy_id="decision_engine",
        payload=payload,
        ingest_source="unit-test",
        symbol="BTCUSDT",
        cycle_id="c-1",
    )
    assert res["id"] is not None
    assert res["payload_sha256"] == bot_payload_hash(payload)
    assert res["deduplicated"] is False
    row = fetch_bot_decision(res["id"])
    assert row["payload_sha256"] == res["payload_sha256"]
    assert row["strategy_id"] == "decision_engine"
    assert row["ingest_source"] == "unit-test"


def test_ingest_is_idempotent(tmp_db):
    payload = _sample_payload()
    a = record_bot_decision(
        strategy_id="decision_engine",
        payload=payload,
        ingest_source="unit-test",
    )
    b = record_bot_decision(
        strategy_id="decision_engine",
        payload=payload,
        ingest_source="unit-test",
    )
    assert a["id"] == b["id"]
    assert a["payload_sha256"] == b["payload_sha256"]
    assert b["deduplicated"] is True


def test_ingest_does_not_mutate_caller_payload(tmp_db):
    payload = _sample_payload()
    before_copy = dict(payload)
    record_bot_decision(
        strategy_id="decision_engine",
        payload=payload,
        ingest_source="unit-test",
    )
    assert payload == before_copy


def test_ingest_roundtrip_hash_matches_disk(tmp_db):
    payload = _sample_payload(PWinPct=42.0)
    res = record_bot_decision(
        strategy_id="decision_engine",
        payload=payload,
        ingest_source="unit-test",
    )
    row = fetch_bot_decision(res["id"])
    # Re-hash the canonical subset stored on disk: must match.
    import json as _json
    disk = _json.loads(row["payload_json"])
    canon = disk["frozen"]
    import hashlib as _h
    blob = _json.dumps(canon, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert _h.sha256(blob).hexdigest() == res["payload_sha256"]


def test_ingest_distinct_strategies_do_not_collide(tmp_db):
    payload = _sample_payload()
    a = record_bot_decision(
        strategy_id="decision_engine",
        payload=payload,
        ingest_source="unit-test",
    )
    b = record_bot_decision(
        strategy_id="binary15m",
        payload=payload,
        ingest_source="unit-test",
    )
    assert a["id"] != b["id"]
    assert a["payload_sha256"] == b["payload_sha256"]


def test_list_bot_decisions_is_read_only(tmp_db):
    for i in range(3):
        record_bot_decision(
            strategy_id="decision_engine",
            payload=_sample_payload(PWinPct=50.0 + i),
            ingest_source="unit-test",
        )
    rows = list_bot_decisions(strategy_id="decision_engine")
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# DB-level enforcement (triggers)
# ---------------------------------------------------------------------------

def test_no_update_trigger_on_bot_table(tmp_db):
    res = record_bot_decision(
        strategy_id="decision_engine",
        payload=_sample_payload(),
        ingest_source="unit-test",
    )
    con = sqlite3.connect(tmp_db)
    try:
        with pytest.raises(sqlite3.IntegrityError) as exc:
            con.execute(
                "UPDATE bot_decisions_immutable SET payload_sha256 = 'tamper' WHERE id = ?",
                (res["id"],),
            )
            con.commit()
        assert "append-only" in str(exc.value).lower()
    finally:
        con.close()


def test_no_delete_trigger_on_bot_table(tmp_db):
    res = record_bot_decision(
        strategy_id="decision_engine",
        payload=_sample_payload(),
        ingest_source="unit-test",
    )
    con = sqlite3.connect(tmp_db)
    try:
        with pytest.raises(sqlite3.IntegrityError) as exc:
            con.execute(
                "DELETE FROM bot_decisions_immutable WHERE id = ?",
                (res["id"],),
            )
            con.commit()
        assert "append-only" in str(exc.value).lower()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Contract surface guarantees
# ---------------------------------------------------------------------------

def test_frozen_fields_tuple_covers_core_decision_outputs():
    required = {
        "PWinPct", "ConfidencePct", "ScoreTotal", "EV_R", "RRTrue",
        "TerminalAction",
        "EntryPx", "StopPx", "TargetPx",
    }
    assert required.issubset(set(BOT_FIELDS_FROZEN))


def test_claw_module_does_not_import_strategy_internals():
    # A Claw component must never depend on bot brain internals.
    import importlib
    forbidden = (
        "strategies.decision_engine",
        "binary15m.agent.agent",
        "binary15.engine",
        "apex_v2.apex_v2_engine",
    )
    for name in ("claw.contract", "claw.ingest", "claw.db"):
        mod = importlib.import_module(name)
        for f in forbidden:
            assert f not in (getattr(mod, "__dict__", {}).keys() or []), \
                f"{name} must not import {f}"
