"""
Tests for L2 — State Truth Gate + tier execution wiring.

Covers:
  - classifier agreement / disagreement
  - UNSTABLE / DEAD_CHOP reject paths
  - tier permission table
  - TREND_TRANSITION halves size
  - tier toggle fires LAST and preserves analysis_qualified flag
  - no-capital-lock regression guard on L2 source
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from spot_aggro.gates import state_gate as sg
from spot_aggro.gates.state_gate import (
    REJ_STATE_DEAD,
    REJ_STATE_UNSTABLE,
    REJ_TIER_NOT_ALLOWED,
    StateTruthGate,
    VERDICT_PASS,
    VERDICT_REJECT,
)
from spot_aggro.gates.state_model import (
    STATE_DEAD_CHOP,
    STATE_POST_SQUEEZE,
    STATE_SQUEEZE_CONFIRMED,
    STATE_TREND_CONFIRMED,
    STATE_TREND_TRANSITION,
    STATE_UNSTABLE,
    StateConfig,
    StateFeatures,
    StateModel,
    classify_squeeze,
    classify_trend,
    fuse_state,
)
from spot_aggro.gates.tier_toggle import TierExecutionToggle


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_state_cfg(tmp_path: Path, **overrides) -> Path:
    base = {
        "schema_version": "spot.state.v1",
        "engine": "spot_aggro",
        "classifier_trend": {
            "adx_trend_confirmed_min": 30.0,
            "adx_transition_min": 20.0,
            "adx_transition_max": 30.0,
            "adx_dead_max": 15.0,
            "hurst_trend_confirmed_min": 0.55,
            "hurst_transition_min": 0.45,
            "hurst_transition_max": 0.55,
            "aligned_candles_required": 3,
        },
        "classifier_squeeze": {
            "bbw_squeeze_pct_max": 20.0,
            "bbw_postsqueeze_expansion_min": 0.05,
            "postsqueeze_max_candles": 3,
            "volume_contraction_required": True,
        },
        "unstable_detector": {
            "classifier_disagreement_max": 0.3,
            "max_transitions_window_s": 600,
            "max_transitions_allowed": 2,
        },
        "permissions": {
            "A+": {"allowed": [STATE_TREND_CONFIRMED, STATE_POST_SQUEEZE]},
            "A":  {"allowed": [STATE_TREND_CONFIRMED, STATE_SQUEEZE_CONFIRMED, STATE_POST_SQUEEZE]},
            "B":  {"allowed": [STATE_TREND_CONFIRMED, STATE_SQUEEZE_CONFIRMED]},
            "C":  {"allowed": [STATE_TREND_CONFIRMED, STATE_POST_SQUEEZE]},
        },
        "size_multiplier": {
            STATE_TREND_CONFIRMED:  1.0,
            STATE_TREND_TRANSITION: 0.5,
            STATE_SQUEEZE_CONFIRMED: 1.0,
            STATE_POST_SQUEEZE:     1.0,
            STATE_DEAD_CHOP:        0.0,
            STATE_UNSTABLE:         0.0,
        },
    }
    # shallow update via nested dict merge on top-level keys
    for k, v in overrides.items():
        base[k] = v
    path = tmp_path / "state.yml"
    path.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
    return path


def _write_tier_cfg(tmp_path: Path, **exec_overrides) -> Path:
    execution = {"A+": True, "A": True, "B": True, "C": True}
    execution.update(exec_overrides)
    payload = {
        "schema_version": "spot.tiers.v1",
        "engine": "spot_aggro",
        "execution": execution,
    }
    path = tmp_path / "tiers.yml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _gate(tmp_path: Path, *, tier_exec: dict[str, bool] | None = None) -> StateTruthGate:
    model = StateModel(config_path=_write_state_cfg(tmp_path))
    toggle = TierExecutionToggle(
        config_path=_write_tier_cfg(tmp_path, **(tier_exec or {}))
    )
    return StateTruthGate(state_model=model, tier_toggle=toggle)


def _feat(**kwargs) -> StateFeatures:
    base = dict(
        symbol="INJ-USDT",
        now_ts=1_700_000_000.0,
        adx=None,
        hurst=None,
        aligned_candle_count=None,
        bbw_percentile_30d=None,
        bbw_expansion_pct=None,
        candles_since_squeeze_break=None,
        volume_contracting=None,
        sigma_realized_percentile=None,
    )
    base.update(kwargs)
    return StateFeatures(**base)


# ---------------------------------------------------------------------------
# Classifier sanity
# ---------------------------------------------------------------------------

def test_trend_classifier_confirms_trend_up(tmp_path: Path) -> None:
    cfg = StateConfig.load(_write_state_cfg(tmp_path))
    c = classify_trend(
        _feat(adx=35.0, hurst=0.60, aligned_candle_count=4), cfg
    )
    assert c.label == "TREND_UP"


def test_trend_classifier_dead(tmp_path: Path) -> None:
    cfg = StateConfig.load(_write_state_cfg(tmp_path))
    c = classify_trend(_feat(adx=10.0, hurst=0.50, aligned_candle_count=0), cfg)
    assert c.label == "TREND_DEAD"


def test_squeeze_classifier_confirmed(tmp_path: Path) -> None:
    cfg = StateConfig.load(_write_state_cfg(tmp_path))
    c = classify_squeeze(
        _feat(bbw_percentile_30d=15.0, volume_contracting=True), cfg
    )
    assert c.label == "SQUEEZE"


def test_squeeze_classifier_post_squeeze(tmp_path: Path) -> None:
    cfg = StateConfig.load(_write_state_cfg(tmp_path))
    c = classify_squeeze(
        _feat(
            bbw_percentile_30d=60.0,
            bbw_expansion_pct=0.10,
            candles_since_squeeze_break=2,
        ), cfg
    )
    assert c.label == "POST_SQUEEZE"


def test_missing_inputs_classify_as_unknown_then_unstable(tmp_path: Path) -> None:
    """One-classifier-missing must force UNSTABLE per spec."""
    cfg = StateConfig.load(_write_state_cfg(tmp_path))
    c1 = classify_trend(_feat(adx=35.0, hurst=0.60, aligned_candle_count=4), cfg)
    c2 = classify_squeeze(_feat(bbw_percentile_30d=None), cfg)
    cls = fuse_state(_feat(), c1, c2, cfg)
    assert cls.state == STATE_UNSTABLE


# ---------------------------------------------------------------------------
# Gate decisions
# ---------------------------------------------------------------------------

def test_rejects_dead_chop(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    d = gate.check(
        _feat(
            adx=10.0, hurst=0.50, aligned_candle_count=0,
            bbw_percentile_30d=50.0, volume_contracting=False,
            sigma_realized_percentile=10.0,
        ),
        tier="A",
    )
    assert d.verdict == VERDICT_REJECT
    assert d.reason_code == REJ_STATE_DEAD


def test_rejects_unstable_on_one_classifier_missing(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    d = gate.check(
        _feat(adx=35.0, hurst=0.60, aligned_candle_count=4, bbw_percentile_30d=None),
        tier="A",
    )
    assert d.verdict == VERDICT_REJECT
    assert d.reason_code == REJ_STATE_UNSTABLE


def test_passes_trend_confirmed_for_tier_b(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    d = gate.check(
        _feat(
            adx=35.0, hurst=0.60, aligned_candle_count=4,
            bbw_percentile_30d=50.0, volume_contracting=False,
        ),
        tier="B",
    )
    assert d.verdict == VERDICT_PASS
    assert d.state == STATE_TREND_CONFIRMED
    assert d.size_multiplier == pytest.approx(1.0)


def test_rejects_tier_when_state_not_permitted(tmp_path: Path) -> None:
    """Tier B has no POST_SQUEEZE in its allowed list."""
    gate = _gate(tmp_path)
    d = gate.check(
        _feat(
            adx=22.0, hurst=0.50, aligned_candle_count=1,
            bbw_percentile_30d=60.0,
            bbw_expansion_pct=0.10,
            candles_since_squeeze_break=1,
        ),
        tier="B",
    )
    assert d.verdict == VERDICT_REJECT
    assert d.reason_code == REJ_TIER_NOT_ALLOWED


def test_trend_transition_halves_size(tmp_path: Path) -> None:
    """ADX mid-band produces TREND_TRANSITION; if a tier permitted it, size
    multiplier would be 0.5. None of the shipped tiers permit TRANSITION,
    so we also verify the gate rejects the trade for tier B."""
    gate = _gate(tmp_path)
    d = gate.check(
        _feat(
            adx=22.0, hurst=0.50, aligned_candle_count=1,
            bbw_percentile_30d=60.0, volume_contracting=False,
        ),
        tier="B",
    )
    assert d.state == STATE_TREND_TRANSITION
    assert d.size_multiplier == pytest.approx(0.5)
    # No tier in default config permits TRANSITION → REJECT
    assert d.verdict == VERDICT_REJECT
    assert d.reason_code == REJ_TIER_NOT_ALLOWED


# ---------------------------------------------------------------------------
# Tier toggle wired at order-permission step
# ---------------------------------------------------------------------------

def test_toggle_blocks_trade_but_preserves_analysis(tmp_path: Path) -> None:
    gate = _gate(tmp_path, tier_exec={"C": False})
    d = gate.check(
        _feat(
            adx=35.0, hurst=0.60, aligned_candle_count=4,
            bbw_percentile_30d=50.0, volume_contracting=False,
        ),
        tier="C",
        analytically_qualified=True,
    )
    assert d.verdict == VERDICT_REJECT
    assert d.reason_code == "TIER_C_TRADE_DISABLED"
    assert d.trade_disabled_by_toggle is True
    assert d.analysis_qualified is True
    assert d.state == STATE_TREND_CONFIRMED     # signal stays observable


def test_toggle_runs_last_after_state_pass(tmp_path: Path) -> None:
    """If state fails FIRST, the toggle is not consulted and the rejection
    is STA-00X, not TIER_*_TRADE_DISABLED."""
    gate = _gate(tmp_path, tier_exec={"B": False})  # B off
    d = gate.check(
        _feat(
            adx=10.0, hurst=0.50, aligned_candle_count=0,
            bbw_percentile_30d=50.0, volume_contracting=False,
            sigma_realized_percentile=10.0,
        ),
        tier="B",
    )
    # DEAD_CHOP should fire before the tier toggle
    assert d.verdict == VERDICT_REJECT
    assert d.reason_code == REJ_STATE_DEAD
    assert d.trade_disabled_by_toggle is False


def test_toggle_blocks_each_tier_with_correct_code(tmp_path: Path) -> None:
    expected = {
        "A+": "TIER_APLUS_TRADE_DISABLED",
        "A":  "TIER_A_TRADE_DISABLED",
        "B":  "TIER_B_TRADE_DISABLED",
        "C":  "TIER_C_TRADE_DISABLED",
    }
    for tier, code in expected.items():
        gate = _gate(
            tmp_path,
            tier_exec={t: (t != tier) for t in ("A+", "A", "B", "C")},
        )
        feat = _feat(
            adx=35.0, hurst=0.60, aligned_candle_count=4,
            bbw_percentile_30d=50.0, volume_contracting=False,
        )
        d = gate.check(feat, tier=tier, analytically_qualified=True)
        if tier in ("A+", "A", "B", "C"):
            # All tiers permit TREND_CONFIRMED → state pass; only toggle blocks
            assert d.reason_code == code, (tier, d.reason_code)
            assert d.trade_disabled_by_toggle is True


def test_toggle_flip_at_runtime_changes_decision(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    feat = _feat(
        adx=35.0, hurst=0.60, aligned_candle_count=4,
        bbw_percentile_30d=50.0, volume_contracting=False,
    )
    first = gate.check(feat, tier="B", analytically_qualified=True)
    assert first.verdict == VERDICT_PASS

    gate.toggle.set_enabled("B", False, actor="ops:test")
    # Feed a fresh feature-tuple (different ts) so classifier history advances
    second = gate.check(
        _feat(
            symbol="INJ-USDT",
            now_ts=1_700_000_060.0,
            adx=35.0, hurst=0.60, aligned_candle_count=4,
            bbw_percentile_30d=50.0, volume_contracting=False,
        ),
        tier="B",
        analytically_qualified=True,
    )
    assert second.verdict == VERDICT_REJECT
    assert second.reason_code == "TIER_B_TRADE_DISABLED"
    assert second.trade_disabled_by_toggle is True


# ---------------------------------------------------------------------------
# Regression: no capital-based gating slipped into L2
# ---------------------------------------------------------------------------

_FORBIDDEN_CODE_TOKENS = (
    "capital_usd", "working_usd", "account_equity", "get_account_equity",
    "deploy_ceil", "CapitalViabilityGate", "CapitalViabilityAdvisory",
    ".capital", ".equity", ".balance", "state.capital",
)


def test_state_gate_source_never_references_capital_or_equity() -> None:
    path = Path(inspect.getsourcefile(sg))
    src = path.read_text(encoding="utf-8")

    tree = ast.parse(src)
    docstring_ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                d = node.body[0]
                docstring_ranges.append((d.lineno, d.end_lineno or d.lineno))

    filtered_lines: list[str] = []
    for idx, line in enumerate(src.splitlines(), start=1):
        if any(lo <= idx <= hi for lo, hi in docstring_ranges):
            continue
        code = line.split("#", 1)[0]
        filtered_lines.append(code)
    code_only = "\n".join(filtered_lines)

    for token in _FORBIDDEN_CODE_TOKENS:
        assert token not in code_only, (
            f"L2 state_gate.py references forbidden capital/equity "
            f"token {token!r}."
        )


def test_state_gate_check_has_no_capital_parameter(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    sig = inspect.signature(gate.check)
    for forbidden in ("capital", "equity", "balance", "account"):
        assert not any(forbidden in p for p in sig.parameters), (
            f"StateTruthGate.check has parameter matching {forbidden!r}"
        )
