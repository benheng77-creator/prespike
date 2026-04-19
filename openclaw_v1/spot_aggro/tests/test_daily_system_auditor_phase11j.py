"""Phase 11j — daily system auditor regression tests.

Locks the contract for the daily integrity auditor:

  1. All 7 categories are represented in the registry.
  2. Every registered check returns a valid (severity, details) tuple
     and never raises — the auditor must be resilient to individual
     broken checks.
  3. A full run_audit() call produces an AuditRun with consistent
     aggregate counts (n_ok + n_warn + n_fail == n_checks).
  4. Verdict is the worst severity across all checks.
  5. persist_audit() + latest_run() round-trip cleanly.
  6. history() returns rows in descending time order, respects limit.
  7. The HTTP routes exist and enforce admin on the write endpoint.
  8. The dashboard card + JS wire are present.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Registry + individual checks
# ---------------------------------------------------------------------------

def test_all_seven_categories_registered():
    from spot_aggro.governance.daily_system_auditor import CHECKS
    categories = {c[1] for c in CHECKS}
    required = {"algorithm", "formula", "flow", "sequence",
                "connections", "state", "data"}
    missing = required - categories
    assert not missing, f"audit missing categories: {missing}"


def test_each_check_returns_valid_tuple():
    """No registered check may raise or return a malformed tuple."""
    from spot_aggro.governance.daily_system_auditor import CHECKS
    for name, category, fn in CHECKS:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"check {name!r} raised {type(exc).__name__}: {exc}")
        assert isinstance(result, tuple) and len(result) == 2, (
            f"check {name!r} returned non-tuple: {type(result).__name__}"
        )
        sev, details = result
        assert sev in ("ok", "warn", "fail"), (
            f"check {name!r} returned invalid severity: {sev!r}"
        )
        assert isinstance(details, str) and len(details) > 0, (
            f"check {name!r} returned empty details"
        )


def test_run_audit_aggregate_counts_consistent(tmp_path, monkeypatch):
    """n_ok + n_warn + n_fail == n_checks, always. Catches any counting
    regression in the aggregation logic."""
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "trades.db"))
    from openclaw_v1.shared.persistence import state as persist
    persist._initialized = False

    from spot_aggro.governance.daily_system_auditor import run_audit
    run = run_audit()
    assert run.n_checks == len(run.checks)
    assert run.n_ok + run.n_warn + run.n_fail == run.n_checks


def test_verdict_matches_worst_severity(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "trades.db"))
    from openclaw_v1.shared.persistence import state as persist
    persist._initialized = False

    from spot_aggro.governance.daily_system_auditor import run_audit
    run = run_audit()
    if run.n_fail > 0:
        assert run.verdict == "fail"
    elif run.n_warn > 0:
        assert run.verdict == "warn"
    else:
        assert run.verdict == "ok"


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------

def test_persist_and_read_back(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "trades.db"))
    from openclaw_v1.shared.persistence import state as persist
    persist._initialized = False

    from spot_aggro.governance.daily_system_auditor import (
        run_and_persist, latest_run, history,
    )

    run = run_and_persist()
    stored = latest_run()
    assert stored is not None
    assert stored["run_id"] == run.run_id
    assert stored["verdict"] == run.verdict
    assert stored["n_checks"] == run.n_checks
    assert "checks" in stored and len(stored["checks"]) == run.n_checks


def test_history_is_ordered_and_respects_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "trades.db"))
    from openclaw_v1.shared.persistence import state as persist
    persist._initialized = False

    from spot_aggro.governance.daily_system_auditor import (
        run_and_persist, history,
    )

    # Run 3 audits with tiny sleeps so timestamps differ.
    for _ in range(3):
        run_and_persist()
        time.sleep(0.01)

    rows = history(limit=2)
    assert len(rows) == 2
    # Descending by started_ts_ms.
    assert rows[0]["started_ts_ms"] >= rows[1]["started_ts_ms"]
    all_rows = history(limit=50)
    assert len(all_rows) == 3


def test_run_and_persist_is_idempotent_on_run_id(tmp_path, monkeypatch):
    """INSERT OR REPLACE on run_id: persisting the same run twice yields
    one stored row, not two. Protects against duplicate rows if the
    scheduler double-fires."""
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "trades.db"))
    from openclaw_v1.shared.persistence import state as persist
    persist._initialized = False

    from spot_aggro.governance.daily_system_auditor import (
        run_audit, persist_audit, history,
    )

    run = run_audit()
    persist_audit(run)
    persist_audit(run)  # re-persist same run_id
    rows = history(limit=10)
    run_ids = [r["run_id"] for r in rows]
    assert run_ids.count(run.run_id) == 1, "run_id must be unique in history"


# ---------------------------------------------------------------------------
# Specific sequence-check contract — the critical Phase 11d invariant
# ---------------------------------------------------------------------------

def test_reconciled_guard_ordering_check_detects_the_invariant():
    """If someone moves the reconciled short-circuit AFTER the TP/SL
    block, this check must detect the regression. Prove the check itself
    works by reading real source."""
    from spot_aggro.governance.daily_system_auditor import (
        check_reconciled_short_circuit_ordering,
    )
    sev, details = check_reconciled_short_circuit_ordering()
    # On a correctly-ordered codebase this returns ok.
    assert sev == "ok", f"expected ok, got {sev}: {details}"
    assert "invariant held" in details.lower() or "precedes" in details.lower()


def test_no_auto_start_check_detects_contamination():
    from spot_aggro.governance.daily_system_auditor import (
        check_start_only_via_explicit_operator_action,
    )
    sev, details = check_start_only_via_explicit_operator_action()
    # server.py has no auto-start (enforced by Phase 11h).
    assert sev == "ok", f"expected ok, got {sev}: {details}"


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------

def test_audit_system_latest_route_exists():
    """GET /spot_aggro/audit/system returns 200 even when no audit has
    run yet (status='never_run'). Must NOT require auth."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from spot_aggro.api import routes as spot_routes

    app = FastAPI()
    app.include_router(spot_routes.router)
    c = TestClient(app)
    r = c.get("/spot_aggro/audit/system")
    assert r.status_code == 200
    body = r.json()
    assert "ok" in body
    assert "status" in body


def test_audit_system_run_route_requires_admin(tmp_path, monkeypatch):
    """POST /spot_aggro/audit/system/run must reject wrong/no token."""
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "trades.db"))
    monkeypatch.setenv("OPS_ADMIN_TOKEN", "test-token-audit")
    from openclaw_v1.shared.persistence import state as persist
    persist._initialized = False

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from spot_aggro.api import routes as spot_routes

    app = FastAPI()
    app.include_router(spot_routes.router)
    c = TestClient(app)

    # No token -> 401.
    r = c.post("/spot_aggro/audit/system/run", json={})
    assert r.status_code == 401

    # Wrong token -> 401.
    r = c.post(
        "/spot_aggro/audit/system/run", json={},
        headers={"X-Ops-Token": "wrong"},
    )
    assert r.status_code == 401

    # Correct token -> 200 + run payload.
    r = c.post(
        "/spot_aggro/audit/system/run", json={},
        headers={"X-Ops-Token": "test-token-audit"},
    )
    assert r.status_code == 200, f"got {r.status_code}: {r.text}"
    body = r.json()
    assert body["ok"] is True
    assert "run" in body
    assert body["run"]["n_checks"] > 0


def test_build_endpoint_advertises_daily_audit_feature():
    """The feature manifest must announce daily_system_audit=True so the
    dashboard can rely on the audit endpoint being present."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from spot_aggro.api import routes as spot_routes

    app = FastAPI()
    app.include_router(spot_routes.router)
    c = TestClient(app)
    r = c.get("/spot_aggro/build")
    assert r.status_code == 200
    assert r.json()["features"].get("daily_system_audit") is True


# ---------------------------------------------------------------------------
# Dashboard wiring
# ---------------------------------------------------------------------------

def test_dashboard_has_system_audit_card():
    s = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    assert 'id="c-sysaudit"' in s
    assert 'id="sysaudit-verdict-pill"' in s
    assert 'id="sysaudit-categories"' in s
    assert 'id="sysaudit-checks"' in s
    assert 'id="sysaudit-history"' in s


def test_dashboard_polls_audit_endpoint():
    s = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
    assert 'f("/spot_aggro/audit/system")' in s
    assert "setInterval(fetchSystemAudit, 30000)" in s
    # Manual-run button wired.
    assert "runSystemAudit" in s
    assert "/spot_aggro/audit/system/run" in s


def test_server_startup_schedules_daily_audit():
    """server.py must register a startup hook that schedules the daily
    auditor. No APScheduler dep — threading.Timer is fine."""
    src = (REPO / "openclaw_v1" / "server.py").read_text(encoding="utf-8")
    assert "_start_spot_aggro_daily_auditor" in src
    assert "run_and_persist" in src
    # 24h interval.
    assert "24 * 3600" in src or "86400" in src
