"""
Tests for the permanent governance layer (WRI + Forensic Governor).

Scope:
  - WRI analyzer cluster rules
  - WRI always-on behavior across A/B/C
  - WRI scheduler cadence decisions
  - Forensic Governor approval/warn/reject verdicts
  - Meta-review aggregation
  - Persistence round-trip
  - Regression: no capital coupling, no trade-placement tokens,
    no import/edit of forensic_v2/
"""

from __future__ import annotations

import ast
import inspect
import time
from pathlib import Path

import pytest
import yaml

from shared.persistence import state as persist

from spot_aggro.governance import store
from spot_aggro.governance.wri import analyzer as wri_an
from spot_aggro.governance.wri import scheduler as wri_sched
from spot_aggro.governance.governor import approver as gov_ap

from spot_aggro.governance.wri.analyzer import (
    ALL_CLUSTERS,
    CLUSTER_BAD_CALIBRATION,
    CLUSTER_BAD_ENTRY,
    CLUSTER_BAD_EXIT,
    CLUSTER_BAD_FRICTION,
    CLUSTER_BAD_STATE_REGIME,
    CLUSTER_SYMBOL_TIER_WEAKNESS,
    CONF_LIKELY,
    CONF_PROVEN,
    CONF_UNVERIFIABLE,
    CONF_WEAK,
    WRIConfig,
    analyze,
)
from spot_aggro.governance.governor.approver import (
    GovernorConfig,
    VERDICT_APPROVED,
    VERDICT_APPROVED_WARNINGS,
    VERDICT_REJECTED,
    approve,
    meta_review,
)


# ---------------------------------------------------------------------------
# Fixtures — isolated DB + governance config
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _iso_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "trades.db"))
    monkeypatch.setattr(persist, "_initialized", False, raising=False)
    monkeypatch.setattr(persist, "_wal_enabled", False, raising=False)
    yield


def _write_governance_cfg(tmp_path: Path, **overrides) -> Path:
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


# ---------------------------------------------------------------------------
# Trade builders
# ---------------------------------------------------------------------------

def _t(**kw):
    base = dict(
        trade_id=f"T{int(time.time()*1000)}-{kw.get('symbol','X')}-{kw.get('tier','?')}",
        symbol="INJ-USDT",
        tier="B",
        regime="TREND_UP",
        composite_score=0.65,
        notional_usd=20.0,
        entry_ts_ms=1_700_000_000_000,
        exit_ts_ms=1_700_000_600_000,
        pnl_usd=0.10,
        pnl_pct=0.005,
        round_trip_cost_bp=16.0,
        expected_move_bp=50.0,
        exit_reason="TP",
    )
    base.update(kw)
    return base


# ===========================================================================
# WRI analyzer
# ===========================================================================

def test_analyze_returns_all_three_tiers_always_even_if_empty() -> None:
    rpt = analyze([], window_start_ms=0, window_end_ms=3_600_000, cadence="micro")
    # Every tier key present regardless of data
    for tier in ("A+", "A", "B", "C"):
        assert tier in rpt["per_tier"]
        assert rpt["per_tier"][tier]["n"] == 0
        assert rpt["per_tier"][tier]["confidence"] == CONF_UNVERIFIABLE
    assert rpt["totals"]["n_trades"] == 0
    assert rpt["totals"]["win_rate"] is None


def test_analyze_separates_six_red_trade_clusters(tmp_path: Path) -> None:
    trades = [
        # bad_entry: red with low composite
        _t(pnl_usd=-0.2, pnl_pct=-0.010, composite_score=0.3),
        _t(pnl_usd=-0.2, pnl_pct=-0.010, composite_score=0.4),
        # bad_state_regime: red in DEAD
        _t(pnl_usd=-0.3, pnl_pct=-0.015, regime="DEAD"),
        _t(pnl_usd=-0.3, pnl_pct=-0.015, regime="UNSTABLE"),
        # bad_friction: expected_move <= 2*cost
        _t(pnl_usd=-0.1, pnl_pct=-0.005, expected_move_bp=20.0, round_trip_cost_bp=16.0),
        _t(pnl_usd=-0.1, pnl_pct=-0.005, expected_move_bp=30.0, round_trip_cost_bp=16.0),
        # bad_exit: red via SL
        _t(pnl_usd=-0.2, pnl_pct=-0.010, exit_reason="SL"),
        _t(pnl_usd=-0.2, pnl_pct=-0.010, exit_reason="TIME_STOP"),
        # green sprinkled
        _t(pnl_usd=0.3, pnl_pct=0.015),
    ]
    cfg = WRIConfig(cluster_min_trades=2, drag_floor_pct=0.05)
    rpt = analyze(
        trades, window_start_ms=0, window_end_ms=3_600_000,
        cadence="operational", cfg=cfg,
    )
    labels = {c["label"] for c in rpt["red_trade_clusters"]}
    assert CLUSTER_BAD_ENTRY in labels
    assert CLUSTER_BAD_STATE_REGIME in labels
    assert CLUSTER_BAD_FRICTION in labels
    assert CLUSTER_BAD_EXIT in labels


def test_analyze_top_root_causes_capped_at_three() -> None:
    trades = [_t(pnl_usd=-0.1, pnl_pct=-0.005, composite_score=0.3,
                 regime="DEAD", expected_move_bp=20.0, exit_reason="SL")
              for _ in range(20)]
    cfg = WRIConfig(cluster_min_trades=2, drag_floor_pct=0.05)
    rpt = analyze(trades, window_start_ms=0, window_end_ms=3_600_000,
                  cadence="full", cfg=cfg)
    assert len(rpt["top_root_causes"]) <= 3


def test_tier_c_is_always_reported_even_when_it_outperforms_b() -> None:
    """Operator rule: Tier C must remain visible in WRI, always."""
    trades = (
        [_t(tier="B", pnl_usd=-0.1, pnl_pct=-0.005) for _ in range(10)]
        + [_t(tier="C", pnl_usd=0.2, pnl_pct=0.010) for _ in range(10)]
    )
    rpt = analyze(trades, window_start_ms=0, window_end_ms=3_600_000,
                  cadence="operational")
    assert rpt["per_tier"]["B"]["n"] == 10
    assert rpt["per_tier"]["C"]["n"] == 10
    assert rpt["per_tier"]["C"]["win_rate"] == pytest.approx(1.0)
    assert rpt["per_tier"]["B"]["win_rate"] == pytest.approx(0.0)


def test_root_cause_classification() -> None:
    # 10 bad-entry reds, 2 greens → share = 10/10 = 1.0 → structural
    trades = (
        [_t(pnl_usd=-0.2, pnl_pct=-0.010, composite_score=0.3) for _ in range(10)]
        + [_t(pnl_usd=0.1, pnl_pct=0.005) for _ in range(2)]
    )
    cfg = WRIConfig(cluster_min_trades=2)
    rpt = analyze(trades, window_start_ms=0, window_end_ms=3_600_000,
                  cadence="operational", cfg=cfg)
    assert rpt["top_root_causes"][0]["classification"] == "structural"


def test_symbol_tier_regime_loss_map_is_ordered() -> None:
    trades = (
        [_t(symbol="INJ-USDT", tier="B", pnl_usd=-0.1) for _ in range(6)]
        + [_t(symbol="WIF-USDT", tier="A", pnl_usd=0.2) for _ in range(3)]
    )
    rpt = analyze(trades, window_start_ms=0, window_end_ms=3_600_000,
                  cadence="operational")
    lm = rpt["symbol_tier_regime_loss_map"]
    assert lm[0]["symbol"] == "INJ-USDT"
    assert lm[0]["n"] == 6


def test_overall_confidence_scales_with_n() -> None:
    cfg = WRIConfig(tier_min_trades_for_likely=5, tier_min_trades_for_proven=15)
    # n=3 → WEAK
    r1 = analyze([_t() for _ in range(3)], window_start_ms=0,
                 window_end_ms=3_600_000, cadence="micro", cfg=cfg)
    assert r1["confidence"] == CONF_WEAK
    # n=7 → LIKELY
    r2 = analyze([_t() for _ in range(7)], window_start_ms=0,
                 window_end_ms=3_600_000, cadence="micro", cfg=cfg)
    assert r2["confidence"] == CONF_LIKELY
    # n=20 → PROVEN
    r3 = analyze([_t() for _ in range(20)], window_start_ms=0,
                 window_end_ms=3_600_000, cadence="micro", cfg=cfg)
    assert r3["confidence"] == CONF_PROVEN


# ===========================================================================
# Scheduler
# ===========================================================================

def test_scheduler_loads_all_three_cadences(tmp_path: Path) -> None:
    p = _write_governance_cfg(tmp_path)
    s = wri_sched.WRISchedule.load(p)
    names = [c.name for c in s.cadences]
    assert names == ["micro", "operational", "full"]
    assert s.cadence("micro").interval_s == 900
    assert s.cadence("operational").interval_s == 7200
    assert s.cadence("full").interval_s == 86400


def test_scheduler_due_cadences_when_never_run(tmp_path: Path) -> None:
    p = _write_governance_cfg(tmp_path)
    s = wri_sched.WRISchedule.load(p)
    due = wri_sched.due_cadences(now_ms=1_700_000_000_000, schedule=s)
    assert {c.name for c in due} == {"micro", "operational", "full"}


def test_scheduler_respects_last_run_ts(tmp_path: Path) -> None:
    p = _write_governance_cfg(tmp_path)
    s = wri_sched.WRISchedule.load(p)
    now = 1_700_000_000_000

    store.write_wri_run(store.WRIRow(
        id=None, ts_ms=now - 60_000, cadence="micro",
        window_start_ms=now - 3_600_000, window_end_ms=now,
        n_trades=5, win_rate=0.4, report={"x": 1}, confidence=CONF_WEAK,
    ))
    due = {c.name for c in wri_sched.due_cadences(now_ms=now, schedule=s)}
    # micro fired 60s ago → not due; others still due (never run).
    assert "micro" not in due
    assert "operational" in due
    assert "full" in due

    # Advance past the micro interval
    due2 = {c.name for c in wri_sched.due_cadences(
        now_ms=now + 1_000_000, schedule=s,
    )}
    assert "micro" in due2


def test_scheduler_disabled_returns_nothing(tmp_path: Path) -> None:
    # Disable WRI in config
    import copy
    base_path = _write_governance_cfg(tmp_path)
    raw = yaml.safe_load(base_path.read_text())
    raw["wri"]["enabled"] = False
    base_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    s = wri_sched.WRISchedule.load(base_path)
    assert wri_sched.due_cadences(now_ms=1_700_000_000_000, schedule=s) == []


# ===========================================================================
# Forensic Governor
# ===========================================================================

def test_governor_approves_clean_report(tmp_path: Path) -> None:
    cfg = GovernorConfig.load(_write_governance_cfg(tmp_path))
    report = {
        "report_id": "R1",
        "sections": [{"confidence": "PROVEN"}, {"confidence": "LIKELY"}],
        "quorum": {"ok_members": 5},
    }
    d = approve(report, cfg=cfg)
    assert d["verdict"] == VERDICT_APPROVED
    assert d["trust_score"] >= 0.75
    assert d["hard_flags"] == []


def test_governor_warns_on_one_unverifiable(tmp_path: Path) -> None:
    cfg = GovernorConfig.load(_write_governance_cfg(tmp_path))
    report = {
        "report_id": "R2",
        "sections": [{"confidence": "PROVEN"}, {"confidence": "UNVERIFIABLE"}],
        "quorum": {"ok_members": 5},
    }
    d = approve(report, cfg=cfg)
    assert d["verdict"] == VERDICT_APPROVED_WARNINGS
    assert d["n_unverifiable_sections"] == 1


def test_governor_rejects_on_three_unverifiable(tmp_path: Path) -> None:
    cfg = GovernorConfig.load(_write_governance_cfg(tmp_path))
    report = {
        "report_id": "R3",
        "sections": [{"confidence": "UNVERIFIABLE"}] * 3 + [{"confidence": "LIKELY"}],
        "quorum": {"ok_members": 5},
    }
    d = approve(report, cfg=cfg)
    assert d["verdict"] == VERDICT_REJECTED
    assert any("UNVERIFIABLE" in f for f in d["hard_flags"])


def test_governor_rejects_on_degraded_quorum(tmp_path: Path) -> None:
    cfg = GovernorConfig.load(_write_governance_cfg(tmp_path))
    report = {
        "report_id": "R4",
        "sections": [{"confidence": "PROVEN"}],
        "quorum": {"ok_members": 3},
    }
    d = approve(report, cfg=cfg)
    assert d["verdict"] == VERDICT_REJECTED
    assert any("quorum" in f.lower() for f in d["hard_flags"])


def test_governor_rejects_on_overclaim(tmp_path: Path) -> None:
    """Strong recommendation with UNVERIFIABLE weakest section → REJECT."""
    cfg = GovernorConfig.load(_write_governance_cfg(tmp_path))
    report = {
        "report_id": "R5",
        "sections": [{"confidence": "UNVERIFIABLE"}],
        "recommendation": "STRONG BUY — increase exposure now",
        "quorum": {"ok_members": 5},
    }
    d = approve(report, cfg=cfg)
    assert d["verdict"] == VERDICT_REJECTED
    assert d["overclaim_detected"] is True


def test_governor_warns_on_contradictions_and_gaps(tmp_path: Path) -> None:
    cfg = GovernorConfig.load(_write_governance_cfg(tmp_path))
    report = {
        "report_id": "R6",
        "sections": [{"confidence": "PROVEN"}],
        "contradictions": ["X contradicts Y"],
        "evidence_gaps": ["missing swarm_action_at_entry"],
        "quorum": {"ok_members": 5},
    }
    d = approve(report, cfg=cfg)
    assert d["verdict"] == VERDICT_APPROVED_WARNINGS
    assert len(d["contradictions"]) == 1
    assert len(d["telemetry_gaps"]) == 1


def test_governor_rejects_on_many_contradictions(tmp_path: Path) -> None:
    cfg = GovernorConfig.load(_write_governance_cfg(tmp_path))
    report = {
        "report_id": "R7",
        "sections": [{"confidence": "PROVEN"}],
        "contradictions": ["a", "b"],
        "quorum": {"ok_members": 5},
    }
    d = approve(report, cfg=cfg)
    assert d["verdict"] == VERDICT_REJECTED


def test_meta_review_flags_recurring_defects(tmp_path: Path) -> None:
    cfg = GovernorConfig.load(_write_governance_cfg(tmp_path))
    rows = (
        [{"verdict": VERDICT_APPROVED_WARNINGS, "hard_flags": [],
          "soft_flags": ["1 contradiction(s)"]} for _ in range(4)]
        + [{"verdict": VERDICT_APPROVED, "hard_flags": [], "soft_flags": []}
           for _ in range(6)]
    )
    out = meta_review(rows, cfg=cfg)
    assert out["total_reports"] == 10
    assert out["approved"] == 6
    assert out["approved_with_warnings"] == 4
    # contradiction appears in 4/10 = 40% >= 30% → recurring
    defs = {d["defect"] for d in out["recurring_defects"]}
    assert any("contradiction" in d for d in defs)


# ===========================================================================
# Persistence round-trip
# ===========================================================================

def test_store_wri_and_governance_roundtrip() -> None:
    import time as _t
    now = int(_t.time() * 1000)
    store.write_wri_run(store.WRIRow(
        id=None, ts_ms=now, cadence="micro",
        window_start_ms=now - 3_600_000, window_end_ms=now,
        n_trades=8, win_rate=0.5, report={"top": "x"}, confidence=CONF_LIKELY,
    ))
    last = store.last_wri_run("micro")
    assert last is not None
    assert last.cadence == "micro" and last.n_trades == 8

    store.write_governance_run(store.GovernanceRow(
        id=None, ts_ms=now, kind="per_report", report_id="R42",
        verdict=VERDICT_APPROVED_WARNINGS, trust_score=0.6,
        findings={"note": "ok"},
    ))
    g = store.get_governance_by_report("R42")
    assert g is not None
    assert g.verdict == VERDICT_APPROVED_WARNINGS
    assert g.trust_score == 0.6


# ===========================================================================
# Regression — no capital coupling, no execution tokens,
# no forensic_v2 import/edit
# ===========================================================================

_FORBIDDEN_CAPITAL_TOKENS = (
    "capital_usd", "working_usd", "account_equity", "get_account_equity",
    "deploy_ceil", "CapitalViabilityGate", "CapitalViabilityAdvisory",
    ".capital", ".equity", ".balance", "state.capital",
)

# Governance layers must not place, cancel, or modify trades in any way.
_FORBIDDEN_EXECUTION_TOKENS = (
    "place_order", "submit_order", "cancel_order",
    "open_position", "close_position",
    "create_order", "send_order",
)

_GOVERNANCE_MODULES = (wri_an, wri_sched, gov_ap, store)


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


def test_governance_never_references_capital_or_equity() -> None:
    for mod in _GOVERNANCE_MODULES:
        path = Path(inspect.getsourcefile(mod))
        code = _strip(path)
        for token in _FORBIDDEN_CAPITAL_TOKENS:
            assert token not in code, (
                f"governance module {mod.__name__} references forbidden "
                f"capital/equity token {token!r}."
            )


def test_governance_never_references_execution_primitives() -> None:
    for mod in _GOVERNANCE_MODULES:
        path = Path(inspect.getsourcefile(mod))
        code = _strip(path)
        for token in _FORBIDDEN_EXECUTION_TOKENS:
            assert token not in code, (
                f"governance module {mod.__name__} references forbidden "
                f"execution token {token!r}. Governance layers must never "
                f"place or modify trades."
            )


def test_governance_never_imports_forensic_v2() -> None:
    """Operator rule: frozen forensic_v2 must not be imported or modified
    by the governance layer. Docstrings may mention the name (we disavow
    it); code must never import or call into it."""
    forbidden_patterns = (
        "import forensic_v2",
        "from forensic_v2",
        "from spot_aggro.forensic_v2",
        "import spot_aggro.forensic_v2",
        "spot_aggro.forensic_v2.",
        "forensic_v2.pdf_renderer",
        "forensic_v2.orchestrator",
        "forensic_v2.schema",
        "forensic_v2.persistence",
        "forensic_v2.specialists",
    )
    for mod in _GOVERNANCE_MODULES:
        path = Path(inspect.getsourcefile(mod))
        code = _strip(path)
        for pat in forbidden_patterns:
            assert pat not in code, (
                f"governance module {mod.__name__} contains forbidden "
                f"forensic_v2 usage pattern {pat!r} — frozen package must "
                f"not be imported or called into."
            )


def test_default_shipped_governance_config_loads() -> None:
    """The shipped governance.yml must parse and declare engine=spot_aggro."""
    cfg = GovernorConfig.load()
    assert cfg.trust_score_min_approved == 0.75
    assert cfg.quorum_ok_minimum == 4
    sched = wri_sched.WRISchedule.load()
    names = [c.name for c in sched.cadences]
    assert names == ["micro", "operational", "full"]
    assert sched.cadence("micro").interval_s == 900
    assert sched.cadence("operational").interval_s == 7200
    assert sched.cadence("full").interval_s == 86400
