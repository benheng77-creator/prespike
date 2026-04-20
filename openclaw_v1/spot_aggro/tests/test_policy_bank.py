"""Opportunity Fabric — Sprint 7 tests: Policy bank tiering."""
from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def _iso_bank(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db))
    # Clear any env overrides that could leak between tests.
    for v in ("CONTRARIAN", "DEEP_VALUE", "MOMENTUM",
              "MEAN_REVERSION", "CONTROL"):
        monkeypatch.delenv(f"SPOT_POLICY_TIER_{v}", raising=False)
    import spot_aggro.governance.policy_bank as pb
    importlib.reload(pb)
    pb._init_schema()
    yield db, pb


def test_default_tier_assignments(_iso_bank):
    _, pb = _iso_bank
    assert pb.tier_for("contrarian").tier == "exploratory"
    assert pb.tier_for("deep_value").tier == "exploratory"
    assert pb.tier_for("momentum").tier == "conservative"
    assert pb.tier_for("mean_reversion").tier == "baseline"
    assert pb.tier_for("control").tier == "baseline"


def test_unknown_variant_defaults_to_exploratory(_iso_bank):
    _, pb = _iso_bank
    t = pb.tier_for("brand_new_variant")
    assert t.tier == "exploratory"
    assert t.source == "default"


def test_env_override_wins(_iso_bank, monkeypatch):
    _, pb = _iso_bank
    monkeypatch.setenv("SPOT_POLICY_TIER_MOMENTUM", "exploratory")
    importlib.reload(pb)
    t = pb.tier_for("momentum")
    assert t.tier == "exploratory"
    assert t.source == "env"


def test_db_override_wins_over_default(_iso_bank):
    _, pb = _iso_bank
    r = pb.assign("contrarian", "conservative",
                  actor="Ben", rationale="40 exits + Wilson + SLO passed")
    assert r["ok"] is True
    assert r["old_tier"] == "exploratory"
    assert r["new_tier"] == "conservative"
    t = pb.tier_for("contrarian")
    assert t.tier == "conservative"
    assert t.source == "override"


def test_env_wins_over_db_override(_iso_bank, monkeypatch):
    _, pb = _iso_bank
    pb.assign("contrarian", "conservative", actor="test", rationale="db")
    monkeypatch.setenv("SPOT_POLICY_TIER_CONTRARIAN", "baseline")
    importlib.reload(pb)
    t = pb.tier_for("contrarian")
    assert t.tier == "baseline"
    assert t.source == "env"


def test_assign_rejects_invalid_tier(_iso_bank):
    _, pb = _iso_bank
    r = pb.assign("contrarian", "nuclear", actor="test", rationale="nope")
    assert r["ok"] is False
    assert "invalid tier" in r["error"]


def test_assign_is_idempotent_on_same_tier(_iso_bank):
    _, pb = _iso_bank
    r1 = pb.assign("contrarian", "conservative", actor="t", rationale="a")
    r2 = pb.assign("contrarian", "conservative", actor="t", rationale="b")
    assert r1["ok"] is True
    assert r2.get("unchanged") is True


def test_event_log_captures_promotion_and_demotion(_iso_bank):
    _, pb = _iso_bank
    pb.assign("contrarian", "conservative", actor="Ben", rationale="promoted")
    pb.assign("contrarian", "exploratory", actor="Ben", rationale="rolled back")
    events = pb.events_recent(limit=10)
    # Events are newest first.
    assert events[0]["new_tier"] == "exploratory"
    assert events[0]["old_tier"] == "conservative"
    assert events[1]["new_tier"] == "conservative"
    assert events[1]["old_tier"] == "exploratory"


def test_variants_in_tier_returns_expected_groups(_iso_bank):
    _, pb = _iso_bank
    expl = pb.variants_in_tier("exploratory")
    cons = pb.variants_in_tier("conservative")
    base = pb.variants_in_tier("baseline")
    assert "contrarian" in expl and "deep_value" in expl
    assert "momentum" in cons
    assert "mean_reversion" in base and "control" in base


def test_summary_bucketizes_correctly(_iso_bank):
    _, pb = _iso_bank
    s = pb.summary()
    assert set(s["buckets"].keys()) == {"conservative", "exploratory", "baseline"}
    assert s["counts"]["exploratory"] == 2
    assert s["counts"]["conservative"] == 1
    assert s["counts"]["baseline"] == 2
