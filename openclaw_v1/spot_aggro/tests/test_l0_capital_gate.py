"""
Tests for L0 — Capital Viability Advisory (non-blocking).

SPOT AGGRO ONLY. Do not import these tests into apex_omega.

Operator directive: L0 is advisory. Low capital produces WARN and must NEVER
block startup or trading. These tests enforce that contract.
"""

from __future__ import annotations

import time
from pathlib import Path
from textwrap import dedent

import pytest

from spot_aggro.gates.capital_gate import (
    CapitalConfig,
    CapitalDecision,
    CapitalViabilityAdvisory,
    CapitalViabilityGate,  # back-compat alias
    VERDICT_OK,
    VERDICT_WARN,
    check_viability,
    compute_reference_min_viable_capital,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_cfg(tmp_path: Path, **overrides) -> Path:
    base = {
        "schema_version": "spot.capital.v2",
        "engine": "spot_aggro",
        "mode": "directional",
        "reference_min_viable_capital_usd": 2000.0,
        "avg_notional_usd": 20.0,
        "daily_trade_count": 40,
        "round_trip_cost_bp": 30.0,
        "required_edge_multiple": 2.0,
        "safety_cost_multiple": 0.15,
        "emergency_reserve_pct": 0.05,
        "recheck_interval_days": 30,
        "recheck_on_startup": True,
        "recommendation_when_below_reference": {
            "summary": "below reference",
            "suggestion": "consider delta-neutral",
            "contact": "operator",
        },
    }
    base.update(overrides)

    import yaml

    path = tmp_path / "capital.yml"
    path.write_text(yaml.safe_dump(base), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# compute_reference_min_viable_capital
# ---------------------------------------------------------------------------

def test_operator_reference_binds_when_higher_than_friction(tmp_path: Path) -> None:
    cfg = CapitalConfig.load(_write_cfg(tmp_path))
    d = compute_reference_min_viable_capital(cfg)

    # per_trade_cost = 20 * 30/10000 = 0.06 USD
    # daily_friction = 0.06 * 40 = 2.4 USD
    # friction_reference = 2.4 / 0.15 = 16.0 USD  (trivially small)
    # operator reference = $2000 → binds
    assert d["per_trade_cost_usd"] == pytest.approx(0.06)
    assert d["daily_friction_usd"] == pytest.approx(2.4)
    assert d["friction_reference_usd"] == pytest.approx(16.0)
    assert d["binding_reference"] == "operator_reference"
    assert d["reference_min_viable_capital_usd"] == 2000.0


def test_daily_friction_binds_when_higher_than_operator_reference(
    tmp_path: Path,
) -> None:
    cfg = CapitalConfig.load(
        _write_cfg(
            tmp_path,
            reference_min_viable_capital_usd=100.0,
            avg_notional_usd=500.0,
            daily_trade_count=200,
            round_trip_cost_bp=40.0,
            safety_cost_multiple=0.10,
        )
    )
    d = compute_reference_min_viable_capital(cfg)
    # per_trade = 500 * 40/10000 = 2.0
    # daily    = 2.0 * 200 = 400
    # friction_reference = 400 / 0.10 = 4000  > 100
    assert d["binding_reference"] == "daily_friction"
    assert d["reference_min_viable_capital_usd"] == pytest.approx(4000.0)


def test_zero_safety_cost_multiple_is_rejected(tmp_path: Path) -> None:
    cfg = CapitalConfig.load(
        _write_cfg(tmp_path, safety_cost_multiple=0.0)
    )
    with pytest.raises(ValueError):
        compute_reference_min_viable_capital(cfg)


# ---------------------------------------------------------------------------
# check_viability — verdict contract
# ---------------------------------------------------------------------------

def test_ok_when_capital_meets_reference(tmp_path: Path) -> None:
    cfg = CapitalConfig.load(_write_cfg(tmp_path))
    decision = check_viability(current_capital_usd=2500.0, cfg=cfg)

    assert decision.verdict == VERDICT_OK
    assert decision.blocking is False
    assert decision.gap_usd == 0.0
    assert decision.recommendation == ""


def test_ok_at_exact_reference(tmp_path: Path) -> None:
    cfg = CapitalConfig.load(_write_cfg(tmp_path))
    decision = check_viability(current_capital_usd=2000.0, cfg=cfg)
    assert decision.verdict == VERDICT_OK
    assert decision.blocking is False
    assert decision.gap_usd == 0.0


def test_warn_when_below_reference_and_non_blocking(tmp_path: Path) -> None:
    cfg = CapitalConfig.load(_write_cfg(tmp_path))
    decision = check_viability(current_capital_usd=391.66, cfg=cfg)

    assert decision.verdict == VERDICT_WARN
    assert decision.blocking is False
    assert decision.gap_usd == pytest.approx(1608.34, abs=0.01)
    # Reason text must signal advisory intent, not a block
    assert "advisory" in decision.reason.lower()


def test_warn_verdict_never_becomes_fail_or_reject(tmp_path: Path) -> None:
    """Regression guard — the verdict vocabulary is exactly {OK, WARN}.
    Adding FAIL/REJECT would reintroduce the blocking contract the operator
    revoked."""
    cfg = CapitalConfig.load(_write_cfg(tmp_path))
    for cap in (0.01, 1.0, 50.0, 99.99, 100.0, 370.0, 391.66, 1000.0, 1999.99):
        d = check_viability(current_capital_usd=cap, cfg=cfg)
        assert d.verdict in {VERDICT_OK, VERDICT_WARN}
        assert d.blocking is False


def test_very_low_capital_does_not_raise(tmp_path: Path) -> None:
    """$100 must NOT raise, must NOT exit, must NOT block — just WARN."""
    cfg = CapitalConfig.load(_write_cfg(tmp_path))
    decision = check_viability(current_capital_usd=100.0, cfg=cfg)
    assert decision.verdict == VERDICT_WARN
    assert decision.blocking is False


def test_zero_capital_still_returns_warn_and_does_not_raise(
    tmp_path: Path,
) -> None:
    """Edge case: $0.00 capital. Must still return a WARN decision, never raise
    and never flip to a blocking verdict."""
    cfg = CapitalConfig.load(_write_cfg(tmp_path))
    decision = check_viability(current_capital_usd=0.0, cfg=cfg)
    assert decision.verdict == VERDICT_WARN
    assert decision.blocking is False
    assert decision.gap_usd == pytest.approx(2000.0)


def test_warn_recommendation_mentions_delta_neutral(tmp_path: Path) -> None:
    """Advisory still suggests APEX-Omega delta-neutral as an alternative,
    but it's a recommendation, not a routing decision."""
    cfg = CapitalConfig.load(_write_cfg(tmp_path))
    decision = check_viability(current_capital_usd=370.0, cfg=cfg)

    assert decision.verdict == VERDICT_WARN
    rec = decision.recommendation.lower()
    assert "delta" in rec and "neutral" in rec


def test_negative_capital_is_rejected(tmp_path: Path) -> None:
    """Malformed input still raises — not a capital-threshold rejection,
    a programmer-error rejection."""
    cfg = CapitalConfig.load(_write_cfg(tmp_path))
    with pytest.raises(ValueError):
        check_viability(current_capital_usd=-1.0, cfg=cfg)


def test_none_capital_is_rejected(tmp_path: Path) -> None:
    cfg = CapitalConfig.load(_write_cfg(tmp_path))
    with pytest.raises(ValueError):
        check_viability(current_capital_usd=None, cfg=cfg)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def test_missing_required_key_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yml"
    bad.write_text(
        dedent(
            """
            schema_version: spot.capital.v2
            engine: spot_aggro
            mode: directional
            reference_min_viable_capital_usd: 2000.0
            avg_notional_usd: 20.0
            daily_trade_count: 40
            round_trip_cost_bp: 30.0
            required_edge_multiple: 2.0
            safety_cost_multiple: 0.15
            emergency_reserve_pct: 0.05
            # recheck_interval_days missing on purpose
            """
        ).strip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as exc:
        CapitalConfig.load(bad)
    assert "recheck_interval_days" in str(exc.value)


def test_wrong_engine_name_is_rejected(tmp_path: Path) -> None:
    """Separation guardrail: the L0 config must declare engine=spot_aggro.
    Prevents accidental reuse of an apex_omega capital config."""
    with pytest.raises(ValueError):
        CapitalConfig.load(_write_cfg(tmp_path, engine="apex_omega"))


def test_config_file_not_found_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        CapitalConfig.load(tmp_path / "does_not_exist.yml")


# ---------------------------------------------------------------------------
# CapitalViabilityAdvisory lifecycle
# ---------------------------------------------------------------------------

def test_advisory_check_startup_caches_last_decision(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path)
    advisory = CapitalViabilityAdvisory(config_path=cfg_path)
    assert advisory.last_decision is None

    d = advisory.check_startup(current_capital_usd=391.66)
    assert d.verdict == VERDICT_WARN
    assert d.blocking is False
    assert advisory.last_decision is d


def test_advisory_check_startup_never_raises_on_low_capital(
    tmp_path: Path,
) -> None:
    """Contract: even with absurdly low capital, check_startup must not raise
    and must return a WARN decision. No SystemExit, no exception."""
    advisory = CapitalViabilityAdvisory(config_path=_write_cfg(tmp_path))
    # Call must simply return — no pytest.raises wrapper.
    for cap in (0.0, 1.0, 50.0, 100.0, 391.66):
        d = advisory.check_startup(current_capital_usd=cap)
        assert d.verdict == VERDICT_WARN
        assert d.blocking is False


def test_backcompat_alias_points_to_advisory(tmp_path: Path) -> None:
    """`CapitalViabilityGate` is a back-compat alias. Behavior must match
    `CapitalViabilityAdvisory` and must be non-blocking."""
    assert CapitalViabilityGate is CapitalViabilityAdvisory
    gate = CapitalViabilityGate(config_path=_write_cfg(tmp_path))
    d = gate.check_startup(current_capital_usd=100.0)
    assert d.verdict == VERDICT_WARN
    assert d.blocking is False


def test_should_recheck_true_before_first_check(tmp_path: Path) -> None:
    advisory = CapitalViabilityAdvisory(config_path=_write_cfg(tmp_path))
    assert advisory.should_recheck() is True


def test_should_recheck_false_until_interval_elapses(tmp_path: Path) -> None:
    advisory = CapitalViabilityAdvisory(config_path=_write_cfg(tmp_path))
    advisory.check_startup(current_capital_usd=2500.0)

    assert advisory.should_recheck() is False

    almost = advisory.last_decision.checked_at_ts + 29 * 86_400
    assert advisory.should_recheck(now_ts=almost) is False

    later = advisory.last_decision.checked_at_ts + 31 * 86_400
    assert advisory.should_recheck(now_ts=later) is True


def test_recheck_reloads_config_from_disk(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path, reference_min_viable_capital_usd=2000.0)
    advisory = CapitalViabilityAdvisory(config_path=cfg_path)
    first = advisory.check_startup(current_capital_usd=2500.0)
    assert first.verdict == VERDICT_OK

    # Operator raises the reference on disk
    cfg_path.write_text(
        cfg_path.read_text().replace(
            "reference_min_viable_capital_usd: 2000.0",
            "reference_min_viable_capital_usd: 3000.0",
        ),
        encoding="utf-8",
    )

    second = advisory.recheck(current_capital_usd=2500.0)
    assert second.verdict == VERDICT_WARN  # now below the raised reference
    assert second.blocking is False
    assert second.reference_min_viable_capital_usd == 3000.0
    assert second.gap_usd == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# Default shipped config
# ---------------------------------------------------------------------------

def test_default_config_on_disk_is_non_blocking_schema() -> None:
    """Shipped spot_aggro/config/capital.yml must carry the v2 advisory schema
    and the operator reference of $2000. The key must be `reference_*`, not
    `locked_*` — a rename of that key without this guard would signal a
    silent reintroduction of the blocking contract."""
    cfg = CapitalConfig.load()
    assert cfg.engine == "spot_aggro"
    assert cfg.mode == "directional"
    assert cfg.schema_version.startswith("spot.capital.v2")
    assert cfg.reference_min_viable_capital_usd == 2000.0
    assert cfg.required_edge_multiple == 2.0
    assert cfg.recheck_interval_days == 30


def test_default_config_warns_at_current_live_capital_without_blocking() -> None:
    """Regression guard: at the known live equity (~$391) the advisory must
    WARN but NOT raise and NOT flip `blocking` to True."""
    advisory = CapitalViabilityAdvisory()
    d = advisory.check_startup(current_capital_usd=391.66)
    assert d.verdict == VERDICT_WARN
    assert d.blocking is False
    assert d.gap_usd > 1000.0
