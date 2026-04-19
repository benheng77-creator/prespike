"""
Tests for L3 — Calibration (store + engine + gate).

SPOT AGGRO ONLY.

Covers:
  * score_to_decile edge cases
  * bucket aggregation arithmetic
  * status rules: VALID / INVALID / INSUFFICIENT / NON_MONOTONIC
  * persistence round-trip (replace_buckets, lookup, list_buckets)
  * gate decisions for every bucket status + missing bucket
  * regression: no capital/equity/balance tokens in L3 source
  * A/B/C all calibrated identically (no tier-specific hard exclusion)
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from spot_aggro.gates import (
    calibration_engine as cal_engine,
    calibration_gate as cal_gate,
    calibration_store as store,
)
from spot_aggro.gates.calibration_engine import (
    MIN_TRADES_PER_BUCKET,
    build_calibration_table,
    score_to_decile,
)
from spot_aggro.gates.calibration_gate import (
    CalibrationGate,
    REJ_INSUFFICIENT,
    REJ_INVALID,
    REJ_INVALID_INPUT,
    REJ_NO_BUCKET,
    VERDICT_PASS,
    VERDICT_PASS_FLAGGED,
    VERDICT_REJECT,
)
from spot_aggro.gates.calibration_store import (
    BucketRow,
    STATUS_INSUFFICIENT,
    STATUS_INVALID,
    STATUS_NON_MONOTONIC,
    STATUS_VALID,
)
from shared.persistence import state as persist


# ---------------------------------------------------------------------------
# Fixtures — isolated DB per test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trades.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db_file))
    # Reset module globals so re-init picks up the new env
    monkeypatch.setattr(persist, "_initialized", False, raising=False)
    monkeypatch.setattr(persist, "_wal_enabled", False, raising=False)
    yield db_file


# ---------------------------------------------------------------------------
# Decile math
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("score,expected", [
    (0.0, 0), (0.05, 0), (0.1, 1), (0.49, 4), (0.5, 5),
    (0.91, 9), (1.0, 9), (1.5, 9), (-0.1, 0),
])
def test_score_to_decile(score: float, expected: int) -> None:
    assert score_to_decile(score) == expected


def test_score_to_decile_handles_nan() -> None:
    import math
    assert score_to_decile(math.nan) == 0


# ---------------------------------------------------------------------------
# Build from synthetic trade list
# ---------------------------------------------------------------------------

def _synth_trades(
    *,
    tier: str,
    symbol: str = "INJ-USDT",
    regime: str = "TREND_UP",
    composite: float = 0.65,
    n: int,
    mean_pnl_pct: float,
) -> list[dict]:
    """Generate n trades for one bucket with specified mean."""
    out = []
    for i in range(n):
        # alternate around mean to hit both wins and losses when mean != 0
        delta = 0.002 if i % 2 == 0 else -0.001
        pnl = mean_pnl_pct + delta
        out.append({
            "tier": tier, "symbol": symbol, "regime": regime,
            "composite": composite, "pnl_pct": pnl, "pnl_usd": 0.5,
            "ts_ms": 1_700_000_000_000 + i * 60_000,
        })
    return out


def test_valid_bucket_when_enough_trades_and_positive_mean() -> None:
    trades = _synth_trades(tier="B", n=30, mean_pnl_pct=0.005)
    stats = build_calibration_table(trades=trades)
    assert stats.buckets_written == 1
    assert stats.n_valid == 1
    assert stats.n_invalid == 0
    assert stats.n_insufficient == 0

    b = store.lookup("B", "INJ-USDT", "TREND_UP", 6)
    assert b is not None
    assert b.status == STATUS_VALID
    assert b.n_trades == 30
    assert b.mean_pnl_pct > 0


def test_invalid_bucket_when_enough_trades_and_negative_mean() -> None:
    trades = _synth_trades(tier="B", n=30, mean_pnl_pct=-0.002)
    stats = build_calibration_table(trades=trades)
    assert stats.n_invalid == 1
    b = store.lookup("B", "INJ-USDT", "TREND_UP", 6)
    assert b.status == STATUS_INVALID


def test_insufficient_when_under_min() -> None:
    trades = _synth_trades(tier="B", n=MIN_TRADES_PER_BUCKET - 1, mean_pnl_pct=0.010)
    stats = build_calibration_table(trades=trades)
    assert stats.n_insufficient == 1
    b = store.lookup("B", "INJ-USDT", "TREND_UP", 6)
    assert b.status == STATUS_INSUFFICIENT


def test_non_monotonic_flag_set_when_higher_decile_underperforms() -> None:
    """Decile 3 mean > decile 7 mean → decile 7 gets NON_MONOTONIC."""
    low = _synth_trades(
        tier="B", n=25, mean_pnl_pct=0.020, composite=0.35,
    )
    high = _synth_trades(
        tier="B", n=25, mean_pnl_pct=0.010, composite=0.75,
    )
    build_calibration_table(trades=low + high)

    b_low = store.lookup("B", "INJ-USDT", "TREND_UP", 3)
    b_high = store.lookup("B", "INJ-USDT", "TREND_UP", 7)
    assert b_low.status == STATUS_VALID
    assert b_high.status == STATUS_NON_MONOTONIC


def test_non_monotonic_only_compares_to_valid_baselines() -> None:
    """An INVALID baseline should not cause NON_MONOTONIC on a higher bucket."""
    bad_low = _synth_trades(
        tier="B", n=25, mean_pnl_pct=-0.01, composite=0.35,
    )
    good_high = _synth_trades(
        tier="B", n=25, mean_pnl_pct=0.005, composite=0.75,
    )
    build_calibration_table(trades=bad_low + good_high)
    b_high = store.lookup("B", "INJ-USDT", "TREND_UP", 7)
    assert b_high.status == STATUS_VALID  # not NON_MONOTONIC


# ---------------------------------------------------------------------------
# All tiers calibrated
# ---------------------------------------------------------------------------

def test_every_tier_is_included_even_tier_c() -> None:
    """Tier C must appear in the calibration table on equal footing."""
    trades: list[dict] = []
    for tier in ("A+", "A", "B", "C"):
        trades += _synth_trades(tier=tier, n=25, mean_pnl_pct=0.004)
    build_calibration_table(trades=trades)
    for tier in ("A+", "A", "B", "C"):
        b = store.lookup(tier, "INJ-USDT", "TREND_UP", 6)
        assert b is not None, f"tier {tier} not in table"
        assert b.status == STATUS_VALID


def test_list_buckets_returns_all_tiers_regardless_of_status() -> None:
    trades = (
        _synth_trades(tier="A+", n=25, mean_pnl_pct=0.004)
        + _synth_trades(tier="A",  n=25, mean_pnl_pct=-0.002)
        + _synth_trades(tier="B",  n=10, mean_pnl_pct=0.004)  # insufficient
        + _synth_trades(tier="C",  n=25, mean_pnl_pct=0.004)
    )
    build_calibration_table(trades=trades)
    all_rows = store.list_buckets()
    tiers = {r.tier for r in all_rows}
    assert tiers == {"A+", "A", "B", "C"}


# ---------------------------------------------------------------------------
# Gate decisions
# ---------------------------------------------------------------------------

def test_gate_pass_on_valid_bucket() -> None:
    build_calibration_table(
        trades=_synth_trades(tier="B", n=25, mean_pnl_pct=0.005)
    )
    gate = CalibrationGate()
    d = gate.check(
        tier="B", symbol="INJ-USDT", regime="TREND_UP", composite_score=0.65,
    )
    assert d.verdict == VERDICT_PASS
    assert d.reason_code is None
    assert d.bucket_status == STATUS_VALID
    assert d.score_decile == 6


def test_gate_pass_flagged_on_non_monotonic() -> None:
    low = _synth_trades(tier="B", n=25, mean_pnl_pct=0.020, composite=0.35)
    high = _synth_trades(tier="B", n=25, mean_pnl_pct=0.010, composite=0.75)
    build_calibration_table(trades=low + high)

    gate = CalibrationGate()
    d = gate.check(
        tier="B", symbol="INJ-USDT", regime="TREND_UP", composite_score=0.75,
    )
    assert d.verdict == VERDICT_PASS_FLAGGED
    assert d.reason_code == STATUS_NON_MONOTONIC


def test_gate_rejects_insufficient() -> None:
    build_calibration_table(
        trades=_synth_trades(tier="C", n=5, mean_pnl_pct=0.01)
    )
    gate = CalibrationGate()
    d = gate.check(
        tier="C", symbol="INJ-USDT", regime="TREND_UP", composite_score=0.65,
    )
    assert d.verdict == VERDICT_REJECT
    assert d.reason_code == REJ_INSUFFICIENT


def test_gate_rejects_invalid_bucket() -> None:
    build_calibration_table(
        trades=_synth_trades(tier="C", n=25, mean_pnl_pct=-0.005)
    )
    gate = CalibrationGate()
    d = gate.check(
        tier="C", symbol="INJ-USDT", regime="TREND_UP", composite_score=0.65,
    )
    assert d.verdict == VERDICT_REJECT
    assert d.reason_code == REJ_INVALID


def test_gate_rejects_missing_bucket() -> None:
    store.clear_all()
    gate = CalibrationGate()
    d = gate.check(
        tier="C", symbol="DOES-NOT-EXIST",
        regime="TREND_UP", composite_score=0.65,
    )
    assert d.verdict == VERDICT_REJECT
    assert d.reason_code == REJ_NO_BUCKET


def test_gate_rejects_on_invalid_input() -> None:
    gate = CalibrationGate()
    d = gate.check(tier="", symbol="X", regime="R", composite_score=0.5)
    assert d.reason_code == REJ_INVALID_INPUT
    d2 = gate.check(tier="B", symbol="", regime="R", composite_score=0.5)
    assert d2.reason_code == REJ_INVALID_INPUT
    d3 = gate.check(tier="B", symbol="X", regime="R", composite_score=None)  # type: ignore[arg-type]
    assert d3.reason_code == REJ_INVALID_INPUT


def test_gate_tier_c_can_be_valid_and_trades() -> None:
    """Operator rule: Tier C is not hard-banned; positive realized expectancy
    → VALID → PASS. This is the scenario the operator flagged where C beats
    B on win rate in some live snapshots."""
    trades = (
        _synth_trades(tier="B", n=25, mean_pnl_pct=-0.001)
        + _synth_trades(tier="C", n=25, mean_pnl_pct=0.003)
    )
    build_calibration_table(trades=trades)
    gate = CalibrationGate()

    d_b = gate.check(tier="B", symbol="INJ-USDT", regime="TREND_UP", composite_score=0.65)
    d_c = gate.check(tier="C", symbol="INJ-USDT", regime="TREND_UP", composite_score=0.65)
    assert d_b.verdict == VERDICT_REJECT and d_b.reason_code == REJ_INVALID
    assert d_c.verdict == VERDICT_PASS


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------

def test_replace_buckets_is_atomic_overwrite() -> None:
    # First write two rows
    import time as _t
    now = int(_t.time() * 1000)
    rows1 = [
        BucketRow("B", "X", "R", 5, 25, 15, 10, 0.004, 0.1, 2.0, now, STATUS_VALID, now),
        BucketRow("B", "Y", "R", 5, 22, 10, 12, -0.001, -0.022, -0.5, now, STATUS_INVALID, now),
    ]
    store.replace_buckets(rows1)
    assert len(store.list_buckets()) == 2

    # Overwrite with different rows — first set must vanish
    rows2 = [
        BucketRow("C", "Z", "R", 7, 30, 18, 12, 0.005, 0.15, 3.0, now, STATUS_VALID, now),
    ]
    store.replace_buckets(rows2)
    all_rows = store.list_buckets()
    assert len(all_rows) == 1
    assert all_rows[0].tier == "C"


def test_list_buckets_filters() -> None:
    trades = (
        _synth_trades(tier="B", n=25, mean_pnl_pct=0.004)
        + _synth_trades(tier="C", n=25, mean_pnl_pct=-0.002)
    )
    build_calibration_table(trades=trades)
    only_valid = store.list_buckets(status=STATUS_VALID)
    assert all(r.status == STATUS_VALID for r in only_valid)
    only_c = store.list_buckets(tier="C")
    assert all(r.tier == "C" for r in only_c)


# ---------------------------------------------------------------------------
# Regression — no capital-based gating in L3
# ---------------------------------------------------------------------------

_FORBIDDEN_CODE_TOKENS = (
    "capital_usd", "working_usd", "account_equity", "get_account_equity",
    "deploy_ceil", "CapitalViabilityGate", "CapitalViabilityAdvisory",
    ".capital", ".equity", ".balance", "state.capital",
)


def _strip_docstrings_and_comments(path: Path) -> str:
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
    lines = []
    for idx, line in enumerate(src.splitlines(), start=1):
        if any(lo <= idx <= hi for lo, hi in docstring_ranges):
            continue
        lines.append(line.split("#", 1)[0])
    return "\n".join(lines)


def test_l3_source_never_references_capital_or_equity() -> None:
    for mod in (cal_engine, cal_gate, store):
        path = Path(inspect.getsourcefile(mod))
        code = _strip_docstrings_and_comments(path)
        for token in _FORBIDDEN_CODE_TOKENS:
            assert token not in code, (
                f"L3 module {mod.__name__} references forbidden "
                f"capital/equity token {token!r}."
            )


def test_l3_check_has_no_capital_parameter() -> None:
    sig = inspect.signature(CalibrationGate.check)
    for forbidden in ("capital", "equity", "balance", "account"):
        assert not any(forbidden in p for p in sig.parameters), (
            f"CalibrationGate.check has parameter matching {forbidden!r}"
        )
