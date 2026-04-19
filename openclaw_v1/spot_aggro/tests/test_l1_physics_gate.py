"""
Tests for L1 — Trade Physics Gate.

SPOT AGGRO ONLY.

Contract enforced by these tests:
  1. expected_move_bp > k * round_trip_cost_bp            → PASS
  2. expected_move_bp <= k * round_trip_cost_bp           → REJECT PHY-002
  3. expected_move_bp unknown (None / 0 / negative)       → REJECT PHY-001
  4. notional < exchange_min_notional                     → REJECT PHY-004
  5. notional < friction-scaled min_viable                → REJECT PHY-003
  6. NO branch in the gate consults account equity or capital.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from spot_aggro.gates import physics_gate as pg
from spot_aggro.gates.physics_gate import (
    PhysicsConfig,
    PhysicsDecision,
    TradePhysicsGate,
    VERDICT_PASS,
    VERDICT_REJECT,
    REJ_EDGE_BELOW_MULTIPLE,
    REJ_INVALID_INPUT,
    REJ_NOTIONAL_BELOW_EXCH,
    REJ_NOTIONAL_BELOW_FLOOR,
    REJ_UNKNOWN_EXPECTED_MOVE,
    compute_min_viable_notional,
    compute_round_trip_cost_bp,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_cfg(tmp_path: Path, **overrides) -> Path:
    base = {
        "schema_version": "spot.physics.v1",
        "engine": "spot_aggro",
        "required_edge_multiple": 2.0,
        "default_slippage_bp": 3.0,
        "default_fee_bp_taker": 10.0,
        "default_fee_bp_maker": 2.0,
        "post_only_preferred": True,
        "exchange_min_notional_usd": 1.0,
        "symbols": {
            "INJ-USDT": {"half_spread_bp": 3.0, "slippage_bp": 3.0},
        },
    }
    base.update(overrides)
    import yaml

    path = tmp_path / "physics.yml"
    path.write_text(yaml.safe_dump(base), encoding="utf-8")
    return path


def _gate(tmp_path: Path, **overrides) -> TradePhysicsGate:
    return TradePhysicsGate(config_path=_write_cfg(tmp_path, **overrides))


# ---------------------------------------------------------------------------
# Pure arithmetic
# ---------------------------------------------------------------------------

def test_round_trip_cost_is_two_times_components() -> None:
    # 2 * (2 + 3 + 3) = 16 bp
    assert compute_round_trip_cost_bp(2.0, 3.0, 3.0) == pytest.approx(16.0)


@pytest.mark.parametrize("bad", [-0.001, -1.0])
def test_round_trip_cost_rejects_negative(bad: float) -> None:
    with pytest.raises(ValueError):
        compute_round_trip_cost_bp(bad, 3.0, 3.0)
    with pytest.raises(ValueError):
        compute_round_trip_cost_bp(2.0, bad, 3.0)
    with pytest.raises(ValueError):
        compute_round_trip_cost_bp(2.0, 3.0, bad)


def test_min_viable_notional_is_exchange_min_when_edge_geq_1() -> None:
    # expected_move (50bp) >= cost (16bp) → scale = 1 → floor = exchange_min
    floor = compute_min_viable_notional(
        round_trip_cost_bp=16.0,
        expected_move_bp=50.0,
        required_edge_multiple=2.0,
        exchange_min_notional_usd=1.0,
    )
    assert floor == pytest.approx(1.0)


def test_min_viable_notional_scales_up_when_edge_less_than_1() -> None:
    # cost 20bp, expected 10bp → scale = 20/10 = 2 → floor = 2 * 1.0 = 2.0
    floor = compute_min_viable_notional(
        round_trip_cost_bp=20.0,
        expected_move_bp=10.0,
        required_edge_multiple=2.0,
        exchange_min_notional_usd=1.0,
    )
    assert floor == pytest.approx(2.0)


def test_min_viable_notional_rejects_unknown_expected_move() -> None:
    with pytest.raises(ValueError):
        compute_min_viable_notional(
            round_trip_cost_bp=16.0,
            expected_move_bp=0.0,
            required_edge_multiple=2.0,
            exchange_min_notional_usd=1.0,
        )


# ---------------------------------------------------------------------------
# check() — PASS cases
# ---------------------------------------------------------------------------

def test_passes_when_expected_move_strictly_exceeds_k_times_cost(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    # INJ maker fee = 2bp, half=3bp, slip=3bp → cost = 2*(2+3+3) = 16bp
    # k * cost = 32bp. 33bp > 32bp → PASS.
    d = gate.check(
        symbol="INJ-USDT",
        notional_usd=20.0,
        expected_move_bp=33.0,
    )
    assert d.verdict == VERDICT_PASS
    assert d.reason_code is None
    assert d.round_trip_cost_bp == pytest.approx(16.0)
    assert d.edge_multiple_actual == pytest.approx(33.0 / 16.0)


def test_passes_with_taker_fee_when_edge_covers(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    # taker=10, half=3, slip=3 → cost = 32bp → k*cost = 64bp
    d = gate.check(
        symbol="INJ-USDT",
        notional_usd=50.0,
        expected_move_bp=70.0,
        post_only=False,
    )
    assert d.verdict == VERDICT_PASS
    assert d.components["fee_bp"] == pytest.approx(10.0)
    assert d.round_trip_cost_bp == pytest.approx(32.0)


# ---------------------------------------------------------------------------
# check() — REJECT cases
# ---------------------------------------------------------------------------

def test_rejects_when_expected_move_equals_k_times_cost(tmp_path: Path) -> None:
    """Spec uses strict '>', not '>='. Equality must reject."""
    gate = _gate(tmp_path)
    d = gate.check(
        symbol="INJ-USDT",
        notional_usd=20.0,
        expected_move_bp=32.0,  # exactly k * cost
    )
    assert d.verdict == VERDICT_REJECT
    assert d.reason_code == REJ_EDGE_BELOW_MULTIPLE


def test_rejects_when_expected_move_below_k_times_cost(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    d = gate.check(
        symbol="INJ-USDT",
        notional_usd=20.0,
        expected_move_bp=25.0,
    )
    assert d.verdict == VERDICT_REJECT
    assert d.reason_code == REJ_EDGE_BELOW_MULTIPLE


def test_rejects_when_expected_move_is_none(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    d = gate.check(
        symbol="INJ-USDT",
        notional_usd=20.0,
        expected_move_bp=None,
    )
    assert d.verdict == VERDICT_REJECT
    assert d.reason_code == REJ_UNKNOWN_EXPECTED_MOVE


def test_rejects_when_expected_move_is_zero(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    d = gate.check(
        symbol="INJ-USDT",
        notional_usd=20.0,
        expected_move_bp=0.0,
    )
    assert d.verdict == VERDICT_REJECT
    assert d.reason_code == REJ_UNKNOWN_EXPECTED_MOVE


def test_rejects_when_expected_move_is_negative(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    d = gate.check(
        symbol="INJ-USDT",
        notional_usd=20.0,
        expected_move_bp=-5.0,
    )
    assert d.verdict == VERDICT_REJECT
    assert d.reason_code == REJ_UNKNOWN_EXPECTED_MOVE


def test_rejects_when_notional_below_exchange_min(tmp_path: Path) -> None:
    gate = _gate(tmp_path, exchange_min_notional_usd=5.0)
    d = gate.check(
        symbol="INJ-USDT",
        notional_usd=4.99,
        expected_move_bp=100.0,
    )
    assert d.verdict == VERDICT_REJECT
    assert d.reason_code == REJ_NOTIONAL_BELOW_EXCH


def test_rejects_when_notional_below_friction_scaled_floor(tmp_path: Path) -> None:
    """Force edge_multiple < 1 so floor scales above exchange min; send a
    notional below the scaled floor but above exchange min."""
    # maker=2, half=3, slip=3 → cost=16bp. expected=8bp → scale=2 → floor = 2*exch
    # exchange_min=5 → floor=10. PASS edge is FALSE (8bp <= 2*16=32bp anyway).
    # Need an edge-satisfying case with scale > 1. Push k=1.0 and use
    # expected=17bp < cost=16bp? No — 17 > 2*... . Use very low cost scenario.
    gate = _gate(
        tmp_path,
        required_edge_multiple=1.0,
        default_slippage_bp=0.0,
        exchange_min_notional_usd=5.0,
        symbols={"INJ-USDT": {"half_spread_bp": 0.0, "slippage_bp": 0.0}},
    )
    # cost = 2*(maker=2 + 0 + 0) = 4bp. k=1 → threshold=4bp.
    # expected=3bp is below → edge reject. Use expected=5bp (>4) for edge pass,
    # scale = 4/5 = 0.8 → below 1 → scale clamps to 1 → floor=5.
    # That doesn't exercise scaling. Build an asymmetry instead:
    # Force cost > expected but edge k<expected/cost impossible. So edge-pass
    # AND scale>1 can't coexist when k >= 1. With k < 1 it can:
    gate = _gate(
        tmp_path,
        required_edge_multiple=0.25,
        default_slippage_bp=0.0,
        exchange_min_notional_usd=5.0,
        symbols={"INJ-USDT": {"half_spread_bp": 0.0, "slippage_bp": 0.0}},
    )
    # cost=4bp, k=0.25 → threshold=1bp. expected=2bp passes edge.
    # scale = cost/expected = 4/2 = 2 → floor = 5*2 = 10.
    d = gate.check(
        symbol="INJ-USDT",
        notional_usd=7.0,     # above exchange_min=5 but below floor=10
        expected_move_bp=2.0,
    )
    assert d.verdict == VERDICT_REJECT
    assert d.reason_code == REJ_NOTIONAL_BELOW_FLOOR
    assert d.min_viable_notional_usd == pytest.approx(10.0)


def test_rejects_on_negative_notional(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    d = gate.check(
        symbol="INJ-USDT",
        notional_usd=-1.0,
        expected_move_bp=50.0,
    )
    assert d.verdict == VERDICT_REJECT
    assert d.reason_code == REJ_INVALID_INPUT


# ---------------------------------------------------------------------------
# Symbol overrides
# ---------------------------------------------------------------------------

def test_symbol_prior_overrides_globals(tmp_path: Path) -> None:
    gate = _gate(
        tmp_path,
        symbols={"PEPE-USDT": {"half_spread_bp": 7.0, "slippage_bp": 9.0}},
    )
    # maker=2, half=7, slip=9 → cost = 36bp
    c = gate.round_trip_cost_bp_for("PEPE-USDT")
    assert c == pytest.approx(36.0)


def test_unknown_symbol_uses_defaults(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    # default maker=2, half=3, slip=3 → cost=16
    assert gate.round_trip_cost_bp_for("UNKNOWN-USDT") == pytest.approx(16.0)


def test_observed_overrides_beat_priors(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    c = gate.round_trip_cost_bp_for(
        "INJ-USDT",
        observed_half_spread_bp=8.0,
        observed_slippage_bp=5.0,
        post_only=False,  # taker=10
    )
    # cost = 2*(10 + 8 + 5) = 46
    assert c == pytest.approx(46.0)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def test_wrong_engine_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        PhysicsConfig.load(_write_cfg(tmp_path, engine="apex_omega"))


def test_missing_required_key_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yml"
    bad.write_text(
        "schema_version: x\nengine: spot_aggro\n", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        PhysicsConfig.load(bad)


def test_default_shipped_config_loads() -> None:
    cfg = PhysicsConfig.load()
    assert cfg.engine == "spot_aggro"
    assert cfg.required_edge_multiple == 2.0
    # Spot universe priors must exist
    assert "INJ-USDT" in cfg.symbol_priors
    assert "PEPE-USDT" in cfg.symbol_priors


# ---------------------------------------------------------------------------
# Capital-lock regression guards
# ---------------------------------------------------------------------------

# Concrete symbol/attribute references the L1 module must NEVER use.
# Comments/docstrings that mention the word "capital" are fine (and expected,
# since the module documents why it won't do capital checks); real code
# references to these exact identifiers are what we forbid.
_FORBIDDEN_CODE_TOKENS = (
    "capital_usd",
    "working_usd",
    "account_equity",
    "get_account_equity",
    "deploy_ceil",
    "CapitalViabilityGate",
    "CapitalViabilityAdvisory",
    ".capital",
    ".equity",
    ".balance",
    "state.capital",
)


def test_physics_gate_source_never_references_capital_or_equity() -> None:
    """Static guard: the L1 module must not reference capital/equity symbols.
    Scans non-comment, non-docstring source for real identifier usage."""
    import ast

    path = Path(inspect.getsourcefile(pg))
    src = path.read_text(encoding="utf-8")

    # Strip docstrings so narrative text can discuss capital/equity freely.
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
        in_docstring = any(lo <= idx <= hi for lo, hi in docstring_ranges)
        if in_docstring:
            continue
        # Strip inline comments too.
        code = line.split("#", 1)[0]
        filtered_lines.append(code)
    code_only = "\n".join(filtered_lines)

    for token in _FORBIDDEN_CODE_TOKENS:
        assert token not in code_only, (
            f"L1 physics_gate.py references forbidden capital/equity "
            f"token {token!r}. L1 must be per-trade only."
        )


def test_physics_gate_accepts_trade_regardless_of_account_level(tmp_path: Path) -> None:
    """Behavioral guard: the gate accepts exactly the same trade at any
    hypothetical 'account level' — because it never receives one. If the
    API ever grows an account-balance parameter, this test's signature will
    break and the change becomes visible in review."""
    gate = _gate(tmp_path)
    sig = inspect.signature(gate.check)
    for forbidden in ("capital", "equity", "balance", "account"):
        assert not any(forbidden in p for p in sig.parameters), (
            f"TradePhysicsGate.check gained a parameter matching "
            f"{forbidden!r} — capital-based gating would be reintroduced."
        )

    d = gate.check(
        symbol="INJ-USDT",
        notional_usd=20.0,
        expected_move_bp=50.0,
    )
    assert d.verdict == VERDICT_PASS
