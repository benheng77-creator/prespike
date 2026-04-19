"""
Tests for Tier Execution Toggle.

Rules enforced:
  - All four tiers (A+, A, B, C) are preserved; deletion is rejected.
  - `trade_enabled(tier)` returns current in-memory state.
  - Flipping a toggle audits (ts/actor/note) and notifies via log.
  - Disabled tier yields BLOCKED decision with correct TIER_*_TRADE_DISABLED
    code and preserves `analysis_qualified` flag.
  - Persisting rewrites YAML; non-persist leaves disk alone.
  - Reload picks up disk edits and audits the sync.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from spot_aggro.gates.tier_toggle import (
    KNOWN_TIERS,
    REASON_CODES,
    TierExecutionToggle,
    TierToggleConfig,
)


def _write_cfg(tmp_path: Path, execution: dict[str, bool] | None = None) -> Path:
    payload = {
        "schema_version": "spot.tiers.v1",
        "engine": "spot_aggro",
        "execution": execution or {"A+": True, "A": True, "B": True, "C": True},
    }
    path = tmp_path / "tiers.yml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


# --- config validation ------------------------------------------------------

def test_all_known_tiers_required(tmp_path: Path) -> None:
    # Missing C must fail load
    with pytest.raises(ValueError):
        TierToggleConfig.load(
            _write_cfg(tmp_path, execution={"A+": True, "A": True, "B": True})
        )


def test_wrong_engine_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yml"
    path.write_text(
        yaml.safe_dump({
            "schema_version": "spot.tiers.v1",
            "engine": "apex_omega",
            "execution": {"A+": True, "A": True, "B": True, "C": True},
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        TierToggleConfig.load(path)


def test_known_tiers_are_exactly_four() -> None:
    assert set(KNOWN_TIERS) == {"A+", "A", "B", "C"}


def test_reason_codes_complete() -> None:
    for t in KNOWN_TIERS:
        assert t in REASON_CODES
    assert REASON_CODES["A+"] == "TIER_APLUS_TRADE_DISABLED"
    assert REASON_CODES["A"]  == "TIER_A_TRADE_DISABLED"
    assert REASON_CODES["B"]  == "TIER_B_TRADE_DISABLED"
    assert REASON_CODES["C"]  == "TIER_C_TRADE_DISABLED"


# --- read behaviour ---------------------------------------------------------

def test_all_tiers_default_enabled(tmp_path: Path) -> None:
    t = TierExecutionToggle(config_path=_write_cfg(tmp_path))
    for tier in KNOWN_TIERS:
        assert t.trade_enabled(tier) is True
    assert t.snapshot() == {"A+": True, "A": True, "B": True, "C": True}


def test_unknown_tier_returns_false_defensively(tmp_path: Path) -> None:
    t = TierExecutionToggle(config_path=_write_cfg(tmp_path))
    assert t.trade_enabled("D") is False
    assert t.trade_enabled("") is False


def test_decision_allow_when_enabled(tmp_path: Path) -> None:
    t = TierExecutionToggle(config_path=_write_cfg(tmp_path))
    d = t.decision_for("C", qualifying=True)
    assert d.verdict == "ALLOW"
    assert d.reason_code is None
    assert d.trade_enabled is True
    assert d.analysis_qualified is True


def test_decision_blocked_when_disabled_preserves_qualification(tmp_path: Path) -> None:
    t = TierExecutionToggle(
        config_path=_write_cfg(
            tmp_path,
            execution={"A+": True, "A": True, "B": False, "C": True},
        )
    )
    d = t.decision_for("B", qualifying=True)
    assert d.verdict == "BLOCKED"
    assert d.reason_code == "TIER_B_TRADE_DISABLED"
    assert d.trade_enabled is False
    assert d.analysis_qualified is True


def test_decision_blocked_without_qualification(tmp_path: Path) -> None:
    t = TierExecutionToggle(
        config_path=_write_cfg(
            tmp_path,
            execution={"A+": True, "A": True, "B": False, "C": True},
        )
    )
    d = t.decision_for("B", qualifying=False)
    assert d.verdict == "BLOCKED"
    assert d.analysis_qualified is False
    assert d.reason_code == "TIER_B_TRADE_DISABLED"


def test_each_tier_has_distinct_reason_code(tmp_path: Path) -> None:
    t = TierExecutionToggle(
        config_path=_write_cfg(
            tmp_path,
            execution={"A+": False, "A": False, "B": False, "C": False},
        )
    )
    codes = {t.decision_for(tier, qualifying=True).reason_code for tier in KNOWN_TIERS}
    assert codes == {
        "TIER_APLUS_TRADE_DISABLED",
        "TIER_A_TRADE_DISABLED",
        "TIER_B_TRADE_DISABLED",
        "TIER_C_TRADE_DISABLED",
    }


# --- write behaviour --------------------------------------------------------

def test_set_enabled_updates_state_and_audits(tmp_path: Path) -> None:
    t = TierExecutionToggle(config_path=_write_cfg(tmp_path))
    assert t.trade_enabled("C") is True
    entry = t.set_enabled("C", False, actor="ops:ben", note="exp 1")
    assert t.trade_enabled("C") is False
    assert entry.tier == "C"
    assert entry.old_value is True and entry.new_value is False
    assert entry.actor == "ops:ben"
    log = t.audit_log()
    assert len(log) == 1 and log[0].tier == "C"


def test_set_enabled_unknown_tier_raises(tmp_path: Path) -> None:
    t = TierExecutionToggle(config_path=_write_cfg(tmp_path))
    with pytest.raises(ValueError):
        t.set_enabled("Z", False, actor="ops:ben")


def test_set_enabled_does_not_persist_by_default(tmp_path: Path) -> None:
    path = _write_cfg(tmp_path)
    t = TierExecutionToggle(config_path=path)
    t.set_enabled("B", False, actor="ops:ben")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    # Unchanged on disk
    assert raw["execution"]["B"] is True


def test_set_enabled_persist_true_rewrites_yaml(tmp_path: Path) -> None:
    path = _write_cfg(tmp_path)
    t = TierExecutionToggle(config_path=path)
    t.set_enabled("B", False, actor="ops:ben", persist=True)

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["execution"]["B"] is False
    # Other tiers untouched
    assert raw["execution"]["A"] is True
    assert raw["execution"]["C"] is True


def test_reload_picks_up_disk_changes_and_audits(tmp_path: Path) -> None:
    path = _write_cfg(tmp_path)
    t = TierExecutionToggle(config_path=path)
    assert t.trade_enabled("A+") is True

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["execution"]["A+"] = False
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    t.reload_config()
    assert t.trade_enabled("A+") is False
    logs = t.audit_log()
    assert any(e.tier == "A+" and e.actor == "reload_config" for e in logs)


# --- defaults shipped on disk ----------------------------------------------

def test_default_shipped_config_enables_all_tiers() -> None:
    """Shipped spot_aggro/config/tiers.yml must have all tiers enabled — no
    hidden "Tier C DISABLED at startup" lurking."""
    t = TierExecutionToggle()
    snap = t.snapshot()
    assert snap == {"A+": True, "A": True, "B": True, "C": True}
