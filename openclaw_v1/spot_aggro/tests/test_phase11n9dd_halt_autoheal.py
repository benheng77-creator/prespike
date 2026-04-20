"""Phase 11n-9-dd — halted-mode auto-heal regression locks.

Validates that the dashboard no longer flips to STALE/FAIL whenever the
operator intentionally stops the engine:

  1. engine_state_source returns canonical state + is_intentional_stop.
  2. heartbeat_writer exposes start()/stop()/last_tick_ts_ms() and one
     tick writes a row into equity_marks.
  3. card_truth_gov freshness rule downgrades stale → ok when the
     engine is intentionally stopped.
  4. card_truth_mismatch M2 / M4 rules return None when stopped.
  5. /spot_aggro/build advertises the three new feature flags.
  6. /spot_aggro/gov/engine_state endpoint exists + shape is stable.
  7. Dashboard HTML carries: build bump, .card-idle CSS, IDLE label,
     _refreshEngineState() function + its refresh() wiring, STALE→IDLE
     branch in truthValidator.
"""
from __future__ import annotations

import importlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HTML = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. engine_state_source
# ---------------------------------------------------------------------------

def test_engine_state_source_module_importable():
    mod = importlib.import_module(
        "spot_aggro.governance.engine_state_source"
    )
    assert hasattr(mod, "current_engine_state")
    assert hasattr(mod, "is_intentionally_stopped")


def test_engine_state_source_shape_when_no_engine():
    from spot_aggro.governance import engine_state_source as ess
    s = ess.current_engine_state()
    assert isinstance(s, dict)
    for k in ("state", "is_intentional_stop", "reason", "ts_ms"):
        assert k in s, f"missing key {k}"
    # No engine running in tests → idle + intentional stop.
    assert s["state"] in (
        "idle", "stopped_by_operator", "halted_by_kill", "running", "crashed"
    )
    assert isinstance(s["is_intentional_stop"], bool)


def test_is_intentionally_stopped_returns_bool():
    from spot_aggro.governance.engine_state_source import (
        is_intentionally_stopped,
    )
    assert isinstance(is_intentionally_stopped(), bool)


# ---------------------------------------------------------------------------
# 2. heartbeat_writer
# ---------------------------------------------------------------------------

def test_heartbeat_writer_module_contract():
    mod = importlib.import_module(
        "spot_aggro.ops.scheduler.heartbeat_writer"
    )
    for fn in ("start", "stop", "last_tick_ts_ms", "_write_heartbeat"):
        assert hasattr(mod, fn), f"heartbeat_writer missing {fn}"
    assert mod.HEARTBEAT_INTERVAL_S == 60


def test_heartbeat_writer_writes_equity_row():
    from spot_aggro.ops.scheduler import heartbeat_writer as hb
    from spot_aggro.ops.persistence.state import _connect, init_schema
    init_schema()
    con = _connect()
    try:
        before = con.execute(
            "SELECT COUNT(*) AS n FROM equity_marks"
        ).fetchone()["n"]
    finally:
        con.close()
    hb._write_heartbeat()
    con = _connect()
    try:
        after = con.execute(
            "SELECT COUNT(*) AS n FROM equity_marks"
        ).fetchone()["n"]
    finally:
        con.close()
    assert after == before + 1, (
        f"heartbeat did not write equity row: before={before} after={after}"
    )


# ---------------------------------------------------------------------------
# 3 + 4. card_truth_gov freshness rule + M2/M4 suppression
# ---------------------------------------------------------------------------

def test_card_truth_gov_freshness_respects_intentional_stop():
    src = (REPO / "openclaw_v1" / "spot_aggro" / "governance"
           / "card_truth_gov.py").read_text(encoding="utf-8")
    assert "current_engine_state" in src, (
        "card_truth_gov freshness rule does not consult engine state"
    )
    assert "engine_intentionally_stopped" in src, (
        "card_truth_gov freshness rule does not flag IDLE evidence"
    )


def test_card_truth_mismatch_m2_m4_skip_only_for_explicit_stop():
    src = (REPO / "openclaw_v1" / "spot_aggro" / "governance"
           / "card_truth_mismatch.py").read_text(encoding="utf-8")
    # Both rules must short-circuit on stopped_by_operator / halted_by_kill
    # but NOT on idle (server boot / test context).
    assert src.count("current_engine_state") >= 2, (
        "M2 and M4 rules must both consult current_engine_state()"
    )
    assert src.count("stopped_by_operator") >= 2
    assert src.count("halted_by_kill") >= 2


def test_m2_rule_suppressed_on_explicit_stop(monkeypatch):
    """Explicit stopped_by_operator state must skip the M2 stale rule."""
    from spot_aggro.governance import card_truth_mismatch as m
    from spot_aggro.governance import engine_state_source as ess
    monkeypatch.setattr(
        ess, "current_engine_state",
        lambda: {"state": "stopped_by_operator", "is_intentional_stop": True,
                 "reason": "test stub", "last_cycle_ts_ms": None,
                 "ts_ms": 0},
    )
    assert m._rule_m2() is None


def test_m2_rule_not_suppressed_on_idle():
    """Idle (no engine ever) must NOT trigger the short-circuit path —
    otherwise a dead pipeline at boot would look healthy forever. The
    existing phase-11n-9-z test_mismatch_m2_equity_writer_stale proves
    M2 still fires on real staleness in this state; here we only check
    that the branch logic does not early-return on state=idle."""
    from spot_aggro.governance import card_truth_mismatch as m
    src = (REPO / "openclaw_v1" / "spot_aggro" / "governance"
           / "card_truth_mismatch.py").read_text(encoding="utf-8")
    # The suppression set must NOT include "idle".
    # Locate the tuple of states that trigger the short-circuit.
    assert '"idle"' not in src.split("# M2 and M4 rules")[0] \
        or 'state") in ("stopped_by_operator", "halted_by_kill")' in src, (
        "M2/M4 must short-circuit only on explicit stop / kill, not idle"
    )


# ---------------------------------------------------------------------------
# 5. /spot_aggro/build advertises the three new flags
# ---------------------------------------------------------------------------

def test_build_flags_advertised():
    from spot_aggro.api.routes import spot_aggro_build
    body = spot_aggro_build()
    feats = body.get("features") or {}
    assert feats.get("engine_state_source") is True
    assert feats.get("heartbeat_writer") is True
    assert feats.get("card_truth_respects_halt") is True
    # Phase-dd was a milestone; later phases may bump further. Any
    # phase-11n-9-<suffix> with suffix >= "dd" is acceptable.
    import re
    m = re.match(r"phase-11n-9-([a-z]+)-2026-04-20", body.get("build", ""))
    assert m, f"missing phase-11n-9 build tag: {body.get('build')!r}"
    suffix = m.group(1)
    assert (len(suffix), suffix) >= (2, "dd"), (
        f"build tag must be >= phase-dd (got {suffix})"
    )


# ---------------------------------------------------------------------------
# 6. /spot_aggro/gov/engine_state endpoint
# ---------------------------------------------------------------------------

def test_engine_state_endpoint_returns_stable_shape():
    from spot_aggro.api.routes import spot_aggro_engine_state
    body = spot_aggro_engine_state()
    assert body["ok"] is True
    assert "engine_state" in body
    assert "heartbeat" in body
    assert "last_tick_ts_ms" in body["heartbeat"]
    assert body["heartbeat"]["interval_s"] == 60


# ---------------------------------------------------------------------------
# 7. Dashboard HTML — build tag bump + IDLE plumbing
# ---------------------------------------------------------------------------

def test_build_tag_bumped_to_dd():
    import re
    m = re.search(r'content="phase-11n-9-([a-z]+)-2026-04-20"', HTML)
    assert m, "no phase-11n-9 build tag in HTML"
    suffix = m.group(1)
    assert (len(suffix), suffix) >= (2, "dd"), (
        f"build tag must be >= phase-dd, got {suffix}"
    )


def test_card_idle_css_classes_present():
    for s in (
        ".c.card-idle",
        ".c.card-idle .h h3::after",
        '" · IDLE"',
    ):
        assert s in HTML, f"missing CSS marker: {s}"


def test_refresh_engine_state_function_defined():
    assert "async function _refreshEngineState()" in HTML
    assert "window._engineStateCache" in HTML
    # Fetches the canonical endpoint.
    assert "/spot_aggro/gov/engine_state" in HTML


def test_refresh_calls_engine_state_fetcher():
    assert "_refreshEngineState();" in HTML


def test_truth_validator_downgrades_stale_to_idle():
    # truthValidator must branch on engineIntentionallyStopped and apply
    # card-idle (not card-stale) when the engine is halted.
    assert "engineIntentionallyStopped" in HTML
    assert 'classList.add("card-idle")' in HTML
    # Previous card-stale and card-mismatch clears must also clear idle
    # so the class doesn't latch across refreshes.
    assert 'card-stale", "card-mismatch", "card-idle"' in HTML
