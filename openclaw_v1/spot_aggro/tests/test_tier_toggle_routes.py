"""
Tests for the /spot_aggro/tier_toggles HTTP endpoints (Phase 9d).

Scope:
  - GET returns the current execution state for A+/A/B/C and the audit log.
  - POST requires the admin token and flips a single tier.
  - POST rejects unknown tiers (never lets a tier be removed or invented).
  - State is persisted between requests (singleton survives route calls).
  - Endpoints never touch analytics/funnel/heatmap/forensic paths.
"""

from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path

import pytest
import yaml
import yaml as _yaml  # same lib; alias avoids confusion in fixtures


@pytest.fixture
def isolated_tier_toggle(tmp_path, monkeypatch):
    """Point the tier-toggle config at a tmp YAML so no test mutates the
    shipped file, and reset the module-level singleton in the spot router
    so each test starts from a clean toggle state.

    Phase 9e — the singleton now lives in spot_aggro.api.routes. This
    fixture reflects that move."""
    cfg_path = tmp_path / "tiers.yml"
    cfg_path.write_text(
        yaml.safe_dump({
            "schema_version": "spot.tiers.v1",
            "engine": "spot_aggro",
            "execution": {"A+": True, "A": True, "B": True, "C": True},
        }, sort_keys=False),
        encoding="utf-8",
    )

    # Construct a fresh TierExecutionToggle on the tmp config and inject it
    # as the SPOT router's singleton.
    from spot_aggro.gates.tier_toggle import TierExecutionToggle
    from spot_aggro.api import routes as spot_routes  # type: ignore

    fresh = TierExecutionToggle(config_path=cfg_path)
    monkeypatch.setattr(spot_routes, "_SPOT_TIER_TOGGLE", fresh, raising=False)
    monkeypatch.setenv("OPS_ADMIN_TOKEN", "test-token")
    yield cfg_path


@pytest.fixture
def client(isolated_tier_toggle):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from spot_aggro.api import routes as spot_routes  # type: ignore

    app = FastAPI()
    app.include_router(spot_routes.router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------

def test_get_returns_all_four_tiers_enabled_by_default(client) -> None:
    r = client.get("/spot_aggro/tier_toggles")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["engine"] == "spot_aggro"
    assert set(body["tiers"]) == {"A+", "A", "B", "C"}
    assert body["execution"] == {"A+": True, "A": True, "B": True, "C": True}


def test_get_includes_execution_only_helper_text(client) -> None:
    r = client.get("/spot_aggro/tier_toggles")
    assert "Execution only. Analysis still includes this tier." in r.json()["note"]


def test_get_includes_reason_codes_for_every_tier(client) -> None:
    r = client.get("/spot_aggro/tier_toggles")
    codes = r.json()["reason_codes"]
    assert codes == {
        "A+": "TIER_APLUS_TRADE_DISABLED",
        "A":  "TIER_A_TRADE_DISABLED",
        "B":  "TIER_B_TRADE_DISABLED",
        "C":  "TIER_C_TRADE_DISABLED",
    }


# ---------------------------------------------------------------------------
# POST — auth + happy path
# ---------------------------------------------------------------------------

def test_post_without_token_is_rejected(client) -> None:
    r = client.post("/spot_aggro/tier_toggles",
                    json={"tier": "B", "enabled": False})
    assert r.status_code == 401


def test_post_wrong_token_is_rejected(client) -> None:
    r = client.post(
        "/spot_aggro/tier_toggles",
        headers={"X-Ops-Token": "nope"},
        json={"tier": "B", "enabled": False},
    )
    assert r.status_code == 401


def test_post_flips_tier_and_persists(client) -> None:
    r = client.post(
        "/spot_aggro/tier_toggles",
        headers={"X-Ops-Token": "test-token"},
        json={"tier": "C", "enabled": False, "note": "ops ran test"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["tier"] == "C"
    assert body["old"] is True
    assert body["new"] is False
    assert body["execution"]["C"] is False
    # Subsequent GET reflects the flip
    g = client.get("/spot_aggro/tier_toggles").json()
    assert g["execution"]["C"] is False


def test_audit_log_records_flip(client) -> None:
    client.post(
        "/spot_aggro/tier_toggles",
        headers={"X-Ops-Token": "test-token"},
        json={"tier": "A", "enabled": False, "note": "audit test"},
    )
    g = client.get("/spot_aggro/tier_toggles").json()
    assert len(g["audit"]) >= 1
    latest = g["audit"][0]
    assert latest["tier"] == "A"
    assert latest["old"] is True and latest["new"] is False
    # Actor is a fixed backend label so operator can trace via API.
    assert latest["actor"] == "api:ops_admin"


# ---------------------------------------------------------------------------
# POST — validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tier", ["", "D", "AA", "a+", "zzz"])
def test_post_rejects_unknown_tier(client, tier) -> None:
    r = client.post(
        "/spot_aggro/tier_toggles",
        headers={"X-Ops-Token": "test-token"},
        json={"tier": tier, "enabled": False},
    )
    assert r.status_code == 400
    assert "tier" in r.json()["detail"].lower()


def test_post_accepts_every_canonical_tier(client) -> None:
    for tier in ("A+", "A", "B", "C"):
        r = client.post(
            "/spot_aggro/tier_toggles",
            headers={"X-Ops-Token": "test-token"},
            json={"tier": tier, "enabled": False},
        )
        assert r.status_code == 200, (tier, r.text)


def test_flipping_B_does_not_affect_other_tiers(client) -> None:
    client.post(
        "/spot_aggro/tier_toggles",
        headers={"X-Ops-Token": "test-token"},
        json={"tier": "B", "enabled": False},
    )
    g = client.get("/spot_aggro/tier_toggles").json()
    exec_state = g["execution"]
    assert exec_state["B"] is False
    assert exec_state["A+"] is True
    assert exec_state["A"] is True
    assert exec_state["C"] is True


# ---------------------------------------------------------------------------
# Hard-lock regressions
# ---------------------------------------------------------------------------

_FORBIDDEN_CAPITAL_TOKENS = (
    "capital_usd", "working_usd", "account_equity", "get_account_equity",
    "deploy_ceil", "CapitalViabilityGate", "CapitalViabilityAdvisory",
)


def test_route_handlers_never_reference_capital_or_equity() -> None:
    """Regression guard for the tier-toggle routes only."""
    from spot_aggro.api import routes as spot_routes  # type: ignore

    fns = [
        spot_routes.spot_aggro_tier_toggles,
        spot_routes.spot_aggro_tier_toggles_set,
    ]
    for fn in fns:
        src = inspect.getsource(fn)
        for tok in _FORBIDDEN_CAPITAL_TOKENS:
            assert tok not in src, (
                f"{fn.__name__} references forbidden capital/equity token {tok!r}"
            )


def test_route_handlers_do_not_import_forensic_v2() -> None:
    from spot_aggro.api import routes as spot_routes  # type: ignore

    fns = [
        spot_routes.spot_aggro_tier_toggles,
        spot_routes.spot_aggro_tier_toggles_set,
    ]
    for fn in fns:
        src = inspect.getsource(fn)
        for pat in ("forensic_v2", "spot_aggro.forensic_v2"):
            assert pat not in src, (
                f"{fn.__name__} must not reference frozen forensic_v2 — got {pat!r}"
            )


def _strip_py_docstring_and_comments(src: str) -> str:
    """Remove the leading docstring and inline comments so narrative text
    mentioning a forbidden word (to disavow it) doesn't trip the guard."""
    tree = ast.parse(src)
    doc_ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                d = node.body[0]
                doc_ranges.append((d.lineno, d.end_lineno or d.lineno))
    lines = []
    for idx, line in enumerate(src.splitlines(), start=1):
        if any(lo <= idx <= hi for lo, hi in doc_ranges):
            continue
        lines.append(line.split("#", 1)[0])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase 11n-9-q — apex_omega purge regression guards
# ---------------------------------------------------------------------------

def test_spot_router_uses_spot_aggro_prefix_not_apex() -> None:
    """The spot router must live under /spot_aggro (no /apex)."""
    from spot_aggro.api import routes as spot_routes  # type: ignore
    assert spot_routes.router.prefix == "/spot_aggro", (
        f"spot router prefix must be '/spot_aggro' (got {spot_routes.router.prefix!r})"
    )
    for r in spot_routes.router.routes:
        path = getattr(r, "path", "")
        # The banned substring is '/apex/' (the URL prefix). A path
        # that legitimately includes 'apex' as a noun (e.g. the
        # /gov/apex_purge endpoint added in phase-s) is fine.
        assert "/apex/" not in path, (
            f"spot route {path!r} must not contain /apex/ — apex URL prefix is purged"
        )


def test_ops_router_does_not_expose_tier_toggles() -> None:
    """The /spot_aggro/ops/* router serves shared ops infra only. The
    tier_toggle endpoint is spot-engine-owned and must live on the
    engine router, never on the ops router."""
    from spot_aggro.ops.routes_ops import router as ops_router
    paths = [getattr(r, "path", "") for r in ops_router.routes]
    for p in paths:
        assert "tier_toggles" not in p, (
            f"ops router exposes tier_toggle route {p!r} — must live on engine router"
        )


# Every spot endpoint the dashboard / external callers touch. All of these
# live on the spot engine router under /spot_aggro.
PHASE10_SPOT_PATHS = (
    "/spot_aggro/status",
    "/spot_aggro/start",
    "/spot_aggro/stop",
    "/spot_aggro/funnel",
    "/spot_aggro/forensic_v2/list",
    "/spot_aggro/forensic_v2/{report_id}",
    "/spot_aggro/forensic_v2/{report_id}/pdf",
    "/spot_aggro/forensic_v2/run",
    "/spot_aggro/coin_memory",
    "/spot_aggro/coin_memory/clear",
    "/spot_aggro/swarm",
    "/spot_aggro/stats",
    "/spot_aggro/holdings",
    "/spot_aggro/forensic",
    "/spot_aggro/forensic/{report_id}",
    "/spot_aggro/forensic/{report_id}/pdf",
    "/spot_aggro/tier_toggles",
)


def test_phase10_spot_router_exposes_all_relocated_endpoints() -> None:
    """Every spot endpoint must be present on the spot router with the
    /spot_aggro prefix (no /apex)."""
    from spot_aggro.api import routes as spot_routes  # type: ignore
    spot_paths = {getattr(r, "path", "") for r in spot_routes.router.routes}
    missing = [p for p in PHASE10_SPOT_PATHS if p not in spot_paths]
    assert not missing, f"spot router missing relocated paths: {missing}"


def test_apex_omega_package_is_gone() -> None:
    """apex_omega must not be importable — tree purged in phase-11n-9-q."""
    with pytest.raises(ImportError):
        __import__("apex_omega")


def test_ops_router_keeps_shared_ops_endpoints() -> None:
    """Shared-ops endpoints now live on /spot_aggro/ops/*."""
    from spot_aggro.ops.routes_ops import router as ops_router
    ops_paths = {getattr(r, "path", "") for r in ops_router.routes}
    required = (
        "/spot_aggro/ops/status", "/spot_aggro/ops/trades",
        "/spot_aggro/ops/notifications", "/spot_aggro/ops/pnl",
        "/spot_aggro/ops/kill", "/spot_aggro/ops/llm/cost",
        "/spot_aggro/ops/llm/health", "/spot_aggro/ops/governor",
        "/spot_aggro/ops/watchdog", "/spot_aggro/ops/consensus",
    )
    missing = [p for p in required if p not in ops_paths]
    assert not missing, f"ops router missing shared endpoints: {missing}"


def test_server_mounts_both_ops_and_spot_routers() -> None:
    src = (Path(__file__).resolve().parents[3]
           / "openclaw_v1" / "server.py").read_text(encoding="utf-8")
    assert "from spot_aggro.ops.routes_ops import router as _ops_router" in src
    assert "app.include_router(_ops_router)" in src
    assert "from spot_aggro.api.routes import router as _spot_router" in src
    assert "app.include_router(_spot_router)" in src
    # server.py must not IMPORT apex_omega. Comments that reference
    # the name (e.g. the apex_purge_gov startup hook's docstring) are
    # fine — the governor's whole purpose is to mention it.
    import re as _re
    stripped = _re.sub(r'"""[\s\S]*?"""', "", src)
    stripped = _re.sub(r"#.*", "", stripped)
    assert not _re.search(r"\b(?:from|import)\s+apex_omega\b", stripped), (
        "server.py still imports apex_omega — purge incomplete"
    )


def test_spot_router_exposes_both_tier_toggle_endpoints() -> None:
    from spot_aggro.api import routes as spot_routes  # type: ignore
    methods_by_path: dict[str, set] = {}
    for r in spot_routes.router.routes:
        p = getattr(r, "path", "")
        methods_by_path.setdefault(p, set()).update(getattr(r, "methods", set()))
    # FastAPI stamps the router prefix onto each route's .path attribute, so
    # the canonical path is /spot_aggro/tier_toggles.
    assert "/spot_aggro/tier_toggles" in methods_by_path, methods_by_path
    assert "GET" in methods_by_path["/spot_aggro/tier_toggles"]
    assert "POST" in methods_by_path["/spot_aggro/tier_toggles"]


def test_route_handlers_do_not_touch_analytics_modules() -> None:
    """Execution-only contract: the handlers must only import the tier
    toggle module. They must not reach into scoring, funnel, heatmap,
    coin_memory, calibration, or audit-swarm surfaces."""
    from spot_aggro.api import routes as spot_routes  # type: ignore
    fns = [
        spot_routes.spot_aggro_tier_toggles,
        spot_routes.spot_aggro_tier_toggles_set,
    ]
    forbidden = (
        "coin_memory", "calibration", "scoring", "audit_swarm",
        "funnel", "heatmap", "forensic",
    )
    for fn in fns:
        src = _strip_py_docstring_and_comments(inspect.getsource(fn))
        for tok in forbidden:
            assert tok not in src, (
                f"{fn.__name__} touches analytics surface {tok!r}"
            )
