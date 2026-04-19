"""
Tests for governance runtime (tick + forensic hook).

Drives the engine-wired entry points in isolation, with an injected
trade-loader so no live DB rows are required beyond the isolated test DB.
"""

from __future__ import annotations

import ast
import inspect
import time
from pathlib import Path

import pytest
import yaml

from shared.persistence import state as persist

from spot_aggro.governance import runtime as rt
from spot_aggro.governance import store
from spot_aggro.governance.wri import scheduler as wri_sched
from spot_aggro.governance.governor import approver as gov_ap
from spot_aggro.governance.governor.approver import (
    VERDICT_APPROVED,
    VERDICT_APPROVED_WARNINGS,
    VERDICT_REJECTED,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _iso_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "trades.db"))
    monkeypatch.setattr(persist, "_initialized", False, raising=False)
    monkeypatch.setattr(persist, "_wal_enabled", False, raising=False)
    yield


def _write_cfg(tmp_path: Path, **overrides) -> Path:
    base = {
        "schema_version": "spot.governance.v1",
        "engine": "spot_aggro",
        "wri": {
            "enabled": True,
            "cadences": {
                "micro":       {"interval_s": 900,   "window_s": 3600},
                "operational": {"interval_s": 7200,  "window_s": 28800},
                "full":        {"interval_s": 86400, "window_s": 604800},
            },
            "cluster_min_trades": 2,
            "drag_floor_pct": 0.10,
            "tier_min_trades_for_likely": 5,
            "tier_min_trades_for_proven": 15,
        },
        "governor": {
            "enabled": True,
            "trust_score_min_approved": 0.75,
            "trust_score_min_warnings": 0.40,
            "unverifiable_warn_threshold": 1,
            "unverifiable_reject_threshold": 3,
            "contradiction_warn_threshold": 1,
            "contradiction_reject_threshold": 2,
            "quorum_ok_minimum": 4,
            "meta_review": {
                "enabled": True,
                "interval_s": 86400,
                "window_s": 86400,
                "recurring_defect_fraction": 0.30,
            },
        },
    }
    base.update(overrides)
    p = tmp_path / "governance.yml"
    p.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
    return p


def _t(**kw):
    base = dict(
        trade_id="T1", symbol="INJ-USDT", tier="B", regime="TREND_UP",
        composite_score=0.65, notional_usd=20.0,
        entry_ts_ms=1_700_000_000_000,
        exit_ts_ms=1_700_000_600_000,
        pnl_usd=0.1, pnl_pct=0.005,
        round_trip_cost_bp=16.0, expected_move_bp=50.0,
        exit_reason="TP",
    )
    base.update(kw)
    return base


# ===========================================================================
# tick() — WRI cadence firing
# ===========================================================================

def test_tick_fires_all_cadences_on_first_run(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path)
    schedule = wri_sched.WRISchedule.load(cfg_path)
    gov_cfg = gov_ap.GovernorConfig.load(cfg_path)

    calls = {"n": 0}
    def _loader(win_start, win_end):
        calls["n"] += 1
        return [_t(), _t(pnl_usd=-0.1, pnl_pct=-0.005)]

    now_ms = 1_700_000_000_000
    r = rt.tick(now_ms=now_ms, schedule=schedule,
                governor_cfg=gov_cfg, trade_loader=_loader)
    assert set(r.wri_runs_fired) == {"micro", "operational", "full"}
    assert r.errors == []
    # Trade loader called once per cadence
    assert calls["n"] == 3


def test_tick_writes_wri_rows(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path)
    schedule = wri_sched.WRISchedule.load(cfg_path)
    gov_cfg = gov_ap.GovernorConfig.load(cfg_path)
    loader = lambda a, b: [_t() for _ in range(8)]

    rt.tick(now_ms=1_700_000_000_000, schedule=schedule,
            governor_cfg=gov_cfg, trade_loader=loader)

    micro = store.last_wri_run("micro")
    oper = store.last_wri_run("operational")
    full = store.last_wri_run("full")
    assert micro is not None and micro.n_trades == 8
    assert oper  is not None and oper.n_trades  == 8
    assert full  is not None and full.n_trades  == 8


def test_tick_second_call_within_interval_fires_nothing(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path)
    schedule = wri_sched.WRISchedule.load(cfg_path)
    gov_cfg = gov_ap.GovernorConfig.load(cfg_path)
    loader = lambda a, b: [_t()]

    now = 1_700_000_000_000
    first = rt.tick(now_ms=now, schedule=schedule,
                    governor_cfg=gov_cfg, trade_loader=loader)
    assert set(first.wri_runs_fired) == {"micro", "operational", "full"}

    second = rt.tick(now_ms=now + 60_000, schedule=schedule,
                     governor_cfg=gov_cfg, trade_loader=loader)
    assert second.wri_runs_fired == []


def test_tick_fires_micro_again_after_interval(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path)
    schedule = wri_sched.WRISchedule.load(cfg_path)
    gov_cfg = gov_ap.GovernorConfig.load(cfg_path)
    loader = lambda a, b: [_t()]

    now = 1_700_000_000_000
    rt.tick(now_ms=now, schedule=schedule, governor_cfg=gov_cfg, trade_loader=loader)

    # 16 min later → micro due, others not
    later = rt.tick(now_ms=now + 960_000, schedule=schedule,
                    governor_cfg=gov_cfg, trade_loader=loader)
    assert later.wri_runs_fired == ["micro"]


def test_tick_survives_wri_exception(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path)
    schedule = wri_sched.WRISchedule.load(cfg_path)
    gov_cfg = gov_ap.GovernorConfig.load(cfg_path)

    def _broken_loader(a, b):
        raise RuntimeError("boom")

    r = rt.tick(now_ms=1_700_000_000_000, schedule=schedule,
                governor_cfg=gov_cfg, trade_loader=_broken_loader)
    assert r.wri_runs_fired == []
    assert any("wri:" in e for e in r.errors)


def test_wri_tier_abc_always_present_in_persisted_report(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path)
    schedule = wri_sched.WRISchedule.load(cfg_path)
    gov_cfg = gov_ap.GovernorConfig.load(cfg_path)

    # Feed only Tier C trades — WRI report must still include A+, A, B rows.
    loader = lambda a, b: [_t(tier="C", pnl_usd=0.1, pnl_pct=0.005) for _ in range(5)]

    rt.tick(now_ms=1_700_000_000_000, schedule=schedule,
            governor_cfg=gov_cfg, trade_loader=loader)
    row = store.last_wri_run("micro")
    assert row is not None
    pt = row.report["per_tier"]
    for tier in ("A+", "A", "B", "C"):
        assert tier in pt, f"tier {tier} missing from WRI report"
    assert pt["C"]["n"] == 5
    assert pt["A"]["n"] == 0
    assert pt["B"]["n"] == 0


# ===========================================================================
# Meta-review cadence
# ===========================================================================

def test_meta_fires_on_first_tick_and_not_again_within_interval(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path)
    schedule = wri_sched.WRISchedule.load(cfg_path)
    gov_cfg = gov_ap.GovernorConfig.load(cfg_path)

    r1 = rt.tick(now_ms=1_700_000_000_000, schedule=schedule,
                 governor_cfg=gov_cfg, trade_loader=lambda a, b: [])
    assert r1.governor_meta_fired is True

    # Under 24h later → no meta
    r2 = rt.tick(now_ms=1_700_000_060_000, schedule=schedule,
                 governor_cfg=gov_cfg, trade_loader=lambda a, b: [])
    assert r2.governor_meta_fired is False


def test_meta_fires_again_after_24h(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path)
    schedule = wri_sched.WRISchedule.load(cfg_path)
    gov_cfg = gov_ap.GovernorConfig.load(cfg_path)

    now = 1_700_000_000_000
    rt.tick(now_ms=now, schedule=schedule, governor_cfg=gov_cfg,
            trade_loader=lambda a, b: [])

    later = rt.tick(now_ms=now + 86_400_000 + 1, schedule=schedule,
                    governor_cfg=gov_cfg, trade_loader=lambda a, b: [])
    assert later.governor_meta_fired is True


# ===========================================================================
# on_forensic_report (per-report hook)
# ===========================================================================

def test_on_forensic_report_attaches_governance_block(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path)
    gov_cfg = gov_ap.GovernorConfig.load(cfg_path)

    payload = {
        "report_id": "R-alpha",
        "sections": [{"confidence": "PROVEN"}, {"confidence": "LIKELY"}],
        "quorum": {"ok_members": 5},
    }
    out = rt.on_forensic_report(payload, cfg=gov_cfg)
    assert out is not payload                  # returns a new dict
    assert "governance" not in payload         # original untouched
    assert out["governance"]["verdict"] == VERDICT_APPROVED
    # All original keys still present
    assert out["report_id"] == "R-alpha"
    assert out["sections"] == payload["sections"]


def test_on_forensic_report_rejects_bad_report(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path)
    gov_cfg = gov_ap.GovernorConfig.load(cfg_path)

    payload = {
        "report_id": "R-bad",
        "sections": [{"confidence": "UNVERIFIABLE"}] * 3,
        "quorum": {"ok_members": 2},    # quorum below min
        "recommendation": "STRONG BUY now",
    }
    out = rt.on_forensic_report(payload, cfg=gov_cfg)
    assert out["governance"]["verdict"] == VERDICT_REJECTED


def test_on_forensic_report_persists_row(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path)
    gov_cfg = gov_ap.GovernorConfig.load(cfg_path)

    payload = {
        "report_id": "R-persist",
        "sections": [{"confidence": "LIKELY"}],
        "quorum": {"ok_members": 5},
    }
    rt.on_forensic_report(payload, cfg=gov_cfg)
    row = store.get_governance_by_report("R-persist")
    assert row is not None
    assert row.kind == "per_report"


def test_on_forensic_report_raises_on_non_dict(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path)
    gov_cfg = gov_ap.GovernorConfig.load(cfg_path)
    with pytest.raises(TypeError):
        rt.on_forensic_report("not a dict", cfg=gov_cfg)  # type: ignore[arg-type]


# ===========================================================================
# Regression — runtime hard-locks
# ===========================================================================

_FORBIDDEN_CAPITAL_TOKENS = (
    "capital_usd", "working_usd", "account_equity", "get_account_equity",
    "deploy_ceil", "CapitalViabilityGate", "CapitalViabilityAdvisory",
    ".capital", ".equity", ".balance", "state.capital",
)
_FORBIDDEN_EXECUTION_TOKENS = (
    "place_order", "submit_order", "cancel_order",
    "open_position", "close_position",
    "create_order", "send_order",
)


def _strip(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                d = node.body[0]
                ranges.append((d.lineno, d.end_lineno or d.lineno))
    lines = []
    for idx, line in enumerate(src.splitlines(), start=1):
        if any(lo <= idx <= hi for lo, hi in ranges):
            continue
        lines.append(line.split("#", 1)[0])
    return "\n".join(lines)


def test_runtime_never_references_capital_or_equity() -> None:
    for mod in (rt,):
        path = Path(inspect.getsourcefile(mod))
        code = _strip(path)
        for tok in _FORBIDDEN_CAPITAL_TOKENS:
            assert tok not in code, (
                f"governance runtime references forbidden capital/equity "
                f"token {tok!r}"
            )


def test_runtime_never_references_execution_primitives() -> None:
    for mod in (rt,):
        path = Path(inspect.getsourcefile(mod))
        code = _strip(path)
        for tok in _FORBIDDEN_EXECUTION_TOKENS:
            assert tok not in code, (
                f"governance runtime references forbidden execution token "
                f"{tok!r} — governance must never place or modify trades."
            )


def test_runtime_never_imports_forensic_v2() -> None:
    forbidden = (
        "import forensic_v2", "from forensic_v2",
        "from spot_aggro.forensic_v2", "import spot_aggro.forensic_v2",
        "spot_aggro.forensic_v2.",
    )
    for mod in (rt,):
        path = Path(inspect.getsourcefile(mod))
        code = _strip(path)
        for pat in forbidden:
            assert pat not in code, (
                f"governance runtime contains forbidden forensic_v2 usage "
                f"pattern {pat!r}"
            )


def test_runtime_api_has_no_capital_parameter() -> None:
    for fn in (rt.tick, rt.on_forensic_report):
        sig = inspect.signature(fn)
        for forbidden in ("capital", "equity", "balance", "account"):
            assert not any(forbidden in p for p in sig.parameters), (
                f"{fn.__name__} has parameter matching {forbidden!r}"
            )
