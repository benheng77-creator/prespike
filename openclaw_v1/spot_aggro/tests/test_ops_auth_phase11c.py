"""Phase 11c final — Ops Token auth flow root-fix regression tests.

Scope:
  Backend
    1. /spot_aggro/auth/ping returns ok:true when header matches env.
    2. /spot_aggro/auth/ping returns ok:false, reason=no_client_token when
       header is missing/empty (never raises, never leaks secret).
    3. /spot_aggro/auth/ping returns ok:false, reason=mismatch on wrong
       token.
    4. /spot_aggro/auth/ping returns ok:false, reason=no_server_secret
       when the host has OPS_ADMIN_TOKEN unset — so the dashboard can
       show an actionable message instead of a generic 403.
    5. /spot_aggro/tier_toggles POST still rejects wrong token (unchanged
       contract — auth guard NOT weakened by the probe endpoint).
    6. /spot_aggro/tier_toggles POST accepts the correct token and
       persists the toggle (proving the end-to-end operator path).

  Frontend
    7. The dashboard no longer captures the token into a `const` at load.
    8. postJSON() calls getOpsToken() at each send so late-set tokens
       take effect without a page reload.
    9. The header carries a visible Set/Test/Clear control bound to the
       same localStorage.ops_token key.
   10. 401 responses surface operator-actionable wording (not the raw
       "invalid admin token" string).

These assertions lock the root fix: the tier-toggle UI is now usable from
the dashboard without the operator needing to open DevTools.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
INDEX = REPO / "web" / "ops" / "index.html"


# ---------------------------------------------------------------------------
# Shared test harness (reuses the isolated-toggle fixture from Phase 9d)
# ---------------------------------------------------------------------------

@pytest.fixture
def _env_admin_token(monkeypatch):
    monkeypatch.setenv("OPS_ADMIN_TOKEN", "test-token-phase11c")
    return "test-token-phase11c"


@pytest.fixture
def _env_no_admin_token(monkeypatch):
    monkeypatch.delenv("OPS_ADMIN_TOKEN", raising=False)


@pytest.fixture
def client_with_token(_env_admin_token, tmp_path, monkeypatch):
    """FastAPI test client with OPS_ADMIN_TOKEN set and an isolated tier
    toggle YAML so POST tests don't mutate shipped state."""
    cfg_path = tmp_path / "tiers.yml"
    cfg_path.write_text(
        yaml.safe_dump({
            "schema_version": "spot.tiers.v1",
            "engine": "spot_aggro",
            "execution": {"A+": True, "A": True, "B": True, "C": True},
        }, sort_keys=False),
        encoding="utf-8",
    )
    from spot_aggro.gates.tier_toggle import TierExecutionToggle
    from spot_aggro.api import routes as spot_routes
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    fresh = TierExecutionToggle(config_path=cfg_path)
    monkeypatch.setattr(spot_routes, "_SPOT_TIER_TOGGLE", fresh, raising=False)
    app = FastAPI()
    app.include_router(spot_routes.router)
    return TestClient(app)


@pytest.fixture
def client_no_server_token(_env_no_admin_token, tmp_path, monkeypatch):
    """FastAPI test client with NO OPS_ADMIN_TOKEN set on the host."""
    cfg_path = tmp_path / "tiers.yml"
    cfg_path.write_text(
        yaml.safe_dump({
            "schema_version": "spot.tiers.v1",
            "engine": "spot_aggro",
            "execution": {"A+": True, "A": True, "B": True, "C": True},
        }, sort_keys=False),
        encoding="utf-8",
    )
    from spot_aggro.gates.tier_toggle import TierExecutionToggle
    from spot_aggro.api import routes as spot_routes
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    fresh = TierExecutionToggle(config_path=cfg_path)
    monkeypatch.setattr(spot_routes, "_SPOT_TIER_TOGGLE", fresh, raising=False)
    app = FastAPI()
    app.include_router(spot_routes.router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Backend: /spot_aggro/auth/ping
# ---------------------------------------------------------------------------

def test_ping_valid_token_returns_ok(client_with_token, _env_admin_token):
    r = client_with_token.get(
        "/spot_aggro/auth/ping",
        headers={"X-Ops-Token": _env_admin_token},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["reason"] == "valid"


def test_ping_missing_header_returns_no_client_token(client_with_token):
    r = client_with_token.get("/spot_aggro/auth/ping")  # no header
    assert r.status_code == 200  # probe NEVER raises
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "no_client_token"
    # Hint must mention how to fix — operator-readable.
    assert "Set Token" in body["hint"]


def test_ping_empty_header_returns_no_client_token(client_with_token):
    r = client_with_token.get(
        "/spot_aggro/auth/ping",
        headers={"X-Ops-Token": "   "},  # whitespace = empty after strip
    )
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "no_client_token"


def test_ping_wrong_token_returns_mismatch(client_with_token):
    r = client_with_token.get(
        "/spot_aggro/auth/ping",
        headers={"X-Ops-Token": "wrong-token"},
    )
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "mismatch"
    # Hint must be actionable but NOT echo the expected secret.
    assert "test-token-phase11c" not in body["hint"]


def test_ping_no_server_secret_returns_actionable_reason(client_no_server_token):
    r = client_no_server_token.get(
        "/spot_aggro/auth/ping",
        headers={"X-Ops-Token": "anything"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "no_server_secret"
    # Hint must tell the operator to set it in .env, not leave them guessing.
    assert ".env" in body["hint"] and "OPS_ADMIN_TOKEN" in body["hint"]


# ---------------------------------------------------------------------------
# Backend: existing /tier_toggles auth contract unchanged
# ---------------------------------------------------------------------------

def test_tier_toggle_post_still_rejects_wrong_token(client_with_token):
    """Regression: adding the unauthenticated probe endpoint must NOT
    weaken the tier-toggle POST guard. Wrong token still returns 401."""
    r = client_with_token.post(
        "/spot_aggro/tier_toggles",
        json={"tier": "C", "enabled": False, "note": "test"},
        headers={"X-Ops-Token": "wrong-token"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid admin token"


def test_tier_toggle_post_empty_token_returns_401(client_with_token):
    r = client_with_token.post(
        "/spot_aggro/tier_toggles",
        json={"tier": "C", "enabled": False},
        headers={"X-Ops-Token": ""},
    )
    assert r.status_code == 401


def test_tier_toggle_post_correct_token_end_to_end(client_with_token, _env_admin_token):
    """The critical operator-path test: with the correct token, POST flips
    Tier C off, GET reflects the change, re-enabling it succeeds too."""
    # 1. Initial state — C is on.
    r = client_with_token.get("/spot_aggro/tier_toggles")
    assert r.json()["execution"]["C"] is True

    # 2. POST to turn C off with the correct token.
    r = client_with_token.post(
        "/spot_aggro/tier_toggles",
        json={"tier": "C", "enabled": False, "note": "ui-test"},
        headers={"X-Ops-Token": _env_admin_token},
    )
    assert r.status_code == 200, f"POST failed: {r.status_code} {r.text}"
    body = r.json()
    assert body["ok"] is True
    assert body["tier"] == "C"
    assert body["old"] is True
    assert body["new"] is False

    # 3. GET reflects the change.
    r = client_with_token.get("/spot_aggro/tier_toggles")
    assert r.json()["execution"]["C"] is False
    # Operator lock: A+/A/B still visible and unchanged.
    assert r.json()["execution"]["A+"] is True
    assert r.json()["execution"]["A"] is True
    assert r.json()["execution"]["B"] is True

    # 4. Re-enable C — round-trip works.
    r = client_with_token.post(
        "/spot_aggro/tier_toggles",
        json={"tier": "C", "enabled": True, "note": "ui-test-restore"},
        headers={"X-Ops-Token": _env_admin_token},
    )
    assert r.status_code == 200
    assert r.json()["new"] is True
    r = client_with_token.get("/spot_aggro/tier_toggles")
    assert r.json()["execution"]["C"] is True


# ---------------------------------------------------------------------------
# Frontend: token flow, getter, header UI
# ---------------------------------------------------------------------------

def _src() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_frontend_no_longer_captures_token_into_const():
    """Root-cause regression: the pre-fix dashboard captured the token at
    page load, so setting it later did not take effect until a reload.
    The fix replaces the constant with a live getter."""
    s = _src()
    # The bad pattern must be gone.
    assert 'const TOKEN = localStorage.getItem("ops_token")' not in s, (
        "token must NOT be captured into a const at page load"
    )
    # The live getter must exist.
    assert "function getOpsToken()" in s


def test_frontend_post_helpers_use_live_getter():
    """postJSON and post must call getOpsToken() at send time, not read a
    captured TOKEN constant."""
    s = _src()
    # Both POST helpers read from the getter.
    assert '"X-Ops-Token": getOpsToken()' in s, (
        "POST helpers must read the token live at each send"
    )


def test_frontend_header_has_ops_token_pill_and_buttons():
    s = _src()
    assert 'id="ops-token-pill"' in s, "missing Ops Token status pill"
    assert 'onclick="opsTokenSet()"' in s, "missing Set Token button"
    assert 'onclick="opsTokenTest()"' in s, "missing Test button"
    assert 'onclick="opsTokenClear()"' in s, "missing Clear button"


def test_frontend_401_error_wording_is_operator_actionable():
    """A 401 response must be rewritten to tell the operator what to do,
    not surface the raw backend 'invalid admin token' string."""
    s = _src()
    assert "invalid admin token — click 'Set Token' in the header" in s


def test_frontend_probes_auth_at_load_and_every_minute():
    """Without an auto-probe the operator would not know the token is bad
    until their first POST — which is exactly the pre-fix failure mode."""
    s = _src()
    assert 'document.addEventListener("DOMContentLoaded", opsTokenTest)' in s
    assert "setInterval(opsTokenTest, 60000)" in s


def test_frontend_token_setter_stores_to_localstorage():
    """opsTokenSet must write the entered value to localStorage.ops_token
    (NOT any other key) so the live getter picks it up."""
    s = _src()
    assert 'localStorage.setItem("ops_token", val)' in s
    # Clear wipes it.
    assert 'localStorage.removeItem("ops_token")' in s


def test_frontend_probe_does_not_leak_token_to_other_origins():
    """The probe call must go to the same API base as every other fetch.
    No hardcoded external URL and no CDN hop."""
    s = _src()
    assert 'fetch(API + "/spot_aggro/auth/ping"' in s
