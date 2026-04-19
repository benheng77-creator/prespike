"""Phase 11d final — stuck-heatmap root fixes regression tests.

After 12 hours the operator's heatmap was still showing:
  4 canonical tiers shown · 0 with closed exits · 0 canonical exits
  · 19 canonical activity rows · reconciled: 0 enter / 11 exit
  · ⚠ 470 unknown-provenance rows

Root-cause investigation revealed THREE distinct problems:

  1. The running uvicorn process was started before the Phase 11b API
     patch, so /spot_aggro/ops/trades was still returning the 11-field SELECT
     (no top-level `tier`). The client's payload_json regex fallback
     failed on old reject rows whose payload only had {"error": "..."}
     — no tier buried inside — so those 470 spot rows landed in the
     Unknown-provenance lane.

  2. The reconciled-position exit short-circuit was placed AFTER the
     hard-exit block (TP/SL/TRAIL/TIME_STOP) in _check_exits_v2(). Any
     non-sentinel SL (e.g. a leftover -0.012 from a regression or a
     pre-sentinel position) would close a reconciled position and
     book a real cash loss. Eleven such exits happened historically
     for a total of $-7.91.

  3. No build-version indicator existed on the dashboard, so the
     operator had no way to verify whether their browser was running
     the latest HTML or a stale cached copy.

This file locks the three fixes end-to-end. Contract: any future change
that re-introduces any of these defects trips this test.
"""
from __future__ import annotations

import inspect
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
INDEX = REPO / "web" / "ops" / "index.html"


def _src() -> str:
    return INDEX.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fix 1 — engine exit-path order
# ---------------------------------------------------------------------------

def test_reconciled_short_circuit_is_before_hard_exits():
    """In _check_exits_v2(), the reconciled-position continue MUST appear
    before the TP/SL/TRAIL/TIME_STOP reason assignments. Placement matters:
    if the guard comes after, a non-sentinel SL on a reconciled position
    can still close it and book a cash loss (historical $-7.91 leak)."""
    from spot_aggro import engine as eng_mod
    src = inspect.getsource(eng_mod.SpotAggroEngine._check_exits_v2)

    # Find both anchors.
    guard_idx = src.find('if pos.module in ("M_reconciled"')
    hard_idx = src.find('if ret >= pos.tp:')
    assert guard_idx > 0, "reconciled short-circuit missing from _check_exits_v2"
    assert hard_idx > 0, "hard-exit block missing from _check_exits_v2"
    assert guard_idx < hard_idx, (
        "reconciled short-circuit MUST appear before the TP/SL/TRAIL/"
        "TIME_STOP block — otherwise a non-sentinel SL can close a "
        "reconciled position and book a real cash loss"
    )


def test_reconciled_short_circuit_continues_not_breaks():
    """The guard must `continue` (skip this pos) — never `break` (skip
    the rest of all positions). Regression guard."""
    from spot_aggro import engine as eng_mod
    src = inspect.getsource(eng_mod.SpotAggroEngine._check_exits_v2)
    # Grab the ~3 lines following the guard.
    guard_idx = src.find('if pos.module in ("M_reconciled"')
    tail = src[guard_idx:guard_idx + 200]
    assert "continue" in tail, "reconciled guard must `continue`, not `break`"
    assert "break" not in tail.split("continue")[0], (
        "reconciled guard must not `break` before continuing"
    )


# ---------------------------------------------------------------------------
# Fix 2 — client-side tier derivation fallback chain
# ---------------------------------------------------------------------------

def test_client_tier_resolution_has_three_fallback_tiers():
    """The heatmap aggregation must resolve tier via three ordered fallbacks:
      (a) t.tier top-level field (fresh server)
      (b) payload_json regex (tier buried in payload)
      (c) module-prefix derivation (old reject rows whose payload has no tier)
    This is what makes the dashboard work against a stale server."""
    s = _src()
    # All three fallback tiers must be present in the heatmap aggregation.
    assert "let tier = t.tier;" in s, "tier must read t.tier top-level first"
    assert 'payload.match(/"tier":\\s*"([^"]+)"/)' in s, "must fall back to payload_json regex"
    # Module-pattern fallback — must handle every spot prefix used by the
    # engine AND the DB migration.
    for pattern in (
        'module.startsWith("M3_blitz")',
        'module.startsWith("M1_squeeze_A")',
        'module.startsWith("M1_squeeze")',
        'module.startsWith("M1_flow_B")',
        'module.startsWith("M1_flow")',
        'module.startsWith("M1_scalp_C")',
        'module.startsWith("M1_scalp")',
        'module.startsWith("M_reconciled")',
    ):
        assert pattern in s, f"module-pattern fallback missing: {pattern}"


def test_module_pattern_matches_db_migration():
    """The client-side module-pattern map must match the DB migration
    exactly — otherwise a freshly-logged row categorized by the engine
    would land in a different tier than the same row categorized by the
    client post-facto. Divergence = silent truth break."""
    s = _src()
    migration_src = (REPO / "openclaw_v1" / "shared" / "persistence" /
                     "state.py").read_text(encoding="utf-8")
    # Each of these mappings must appear (in some form) on BOTH sides.
    for module, tier in [
        ("M3_blitz",     '"A+"'),
        ("M1_squeeze_A", '"A"'),
        ("M1_flow_B",    '"B"'),
        ("M1_scalp_C",   '"C"'),
        ("M_reconciled", '"?"'),
    ]:
        # Client side.
        assert (f'module.startsWith("{module}")' in s), (
            f"client missing module→tier map for {module}"
        )
        # Server migration side.
        assert (f'"{module}"' in migration_src), (
            f"server migration missing module pattern for {module}"
        )


# ---------------------------------------------------------------------------
# Fix 3 — build tag + stale-API banner
# ---------------------------------------------------------------------------

def test_dashboard_has_build_version_meta_tag():
    s = _src()
    assert '<meta name="dashboard-build"' in s, (
        "dashboard must carry a build-version meta tag so operators can "
        "confirm they're on a fresh HTML"
    )


def test_build_pill_visible_and_clickable_to_hardreload():
    s = _src()
    assert 'id="build-pill"' in s
    # Click handler must do a hard reload (location.reload(true)) to
    # bypass the browser cache — otherwise the stuck-page problem recurs.
    assert "location.reload(true)" in s


def test_build_pill_populates_from_meta_tag_at_load():
    s = _src()
    assert 'document.querySelector(\'meta[name="dashboard-build"]\')' in s
    assert '"BUILD · "' in s  # prefix on the pill text


def test_stale_api_banner_exists_and_triggers_when_no_tier_field():
    """When every row in /spot_aggro/ops/trades lacks the top-level `tier` field
    (stale server), a banner must be rendered telling the operator to
    restart uvicorn. Without this, the 12h stuck state is invisible."""
    s = _src()
    # Banner element.
    assert 'id="heatmap-stale-api"' in s
    # Trigger condition: every row is missing the tier field.
    assert "allRowsPreFix" in s
    assert "trades.rows.every(r => r && r.tier === undefined)" in s
    # Operator-actionable hint in the banner.
    assert "API outdated" in s
    assert "restart uvicorn" in s.lower() or "restart uvicorn" in s


def test_stale_api_banner_hidden_when_tier_is_present():
    """The opposite of the above — once uvicorn is restarted, the banner
    must hide. The rendering branch must toggle display explicitly."""
    s = _src()
    # Look at the whole banner if/else block (generous window).
    idx = s.find("allRowsPreFix")
    window = s[idx: idx + 1800]
    assert 'staleBanner.style.display = "none"' in window, (
        "stale-API banner must hide when at least one row carries tier"
    )


# ---------------------------------------------------------------------------
# Integration guarantee — all three fixes coexist with Phase 11b/11c
# ---------------------------------------------------------------------------

def test_phase11b_api_still_includes_tier():
    """Fix 1 from Phase 11b (API returns tier top-level) must still be in
    place — the client fallback is defense-in-depth, not a replacement."""
    src = (REPO / "openclaw_v1" / "spot_aggro" / "ops" /
           "routes_ops.py").read_text(encoding="utf-8")
    assert "payload_json, tier" in src


def test_phase11c_auth_ping_still_present():
    """Fix from Phase 11c (auth probe endpoint) must still exist."""
    src = (REPO / "openclaw_v1" / "spot_aggro" / "api" /
           "routes.py").read_text(encoding="utf-8")
    assert "/auth/ping" in src
    assert "no_client_token" in src
    assert "no_server_secret" in src


def test_spot_module_filter_still_present():
    """Fix from Phase 11b (spot-only filter in heatmap) must still be in
    place — without it the perp-engine modules M1_funding / M2_statarb /
    M3_triangular would pollute canonical tier analytics."""
    s = _src()
    assert "SPOT_MODULE_PREFIXES" in s
    assert "isSpotModule" in s


# ---------------------------------------------------------------------------
# Fix 4 — /spot_aggro/ops/trades limit bumped so 24h window is reachable
# ---------------------------------------------------------------------------

def test_apex_trades_limit_cap_raised_to_5000():
    """Default cap of 500 meant the heatmap's declared 24h window was
    unreachable when reject rates were high (500 most recent rows spanned
    ~30 minutes). Cap bumped to 5000 so the full 24h is physically
    available. Without this fix, Lane 1 shows zero canonical exits even
    though 193 exist in the DB."""
    src = (REPO / "openclaw_v1" / "spot_aggro" / "ops" /
           "routes_ops.py").read_text(encoding="utf-8")
    # The Query limit declaration must allow up to 5000.
    assert "le=5000" in src, (
        "/spot_aggro/ops/trades limit cap must be raised from 500 to 5000 so the "
        "heatmap's 24h window is reachable"
    )


def test_heatmap_fetches_with_wide_limit():
    """The dashboard's refresh() call to /spot_aggro/ops/trades must request enough
    rows to populate the 24h window. limit=500 was too narrow."""
    s = _src()
    assert '/spot_aggro/ops/trades?limit=2000' in s, (
        "heatmap must fetch /spot_aggro/ops/trades with limit=2000 so canonical "
        "exits from 24h ago remain visible alongside recent activity"
    )


# ---------------------------------------------------------------------------
# Fix 5 — /spot_aggro/build endpoint + BUILD pill parity check
# ---------------------------------------------------------------------------

def test_server_exposes_build_endpoint():
    """The stuck-12h failure was invisible because neither the operator
    nor the dashboard could tell the server was running pre-Phase-11b
    code. Expose the build tag so the dashboard can cross-check."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from spot_aggro.api import routes as spot_routes

    app = FastAPI()
    app.include_router(spot_routes.router)
    c = TestClient(app)
    r = c.get("/spot_aggro/build")
    assert r.status_code == 200
    body = r.json()
    assert "build" in body
    assert body["build"].startswith("phase-"), (
        "build tag must be a versioned phase marker"
    )
    # Feature manifest must declare which patches are present.
    feats = body.get("features", {})
    for required in (
        "auth_ping", "trades_tier_column",
        "trades_limit_max_5000", "reconciled_short_circuit_top",
    ):
        assert feats.get(required) is True, (
            f"server build manifest missing feature: {required}"
        )


def test_server_build_endpoint_is_unauthenticated():
    """The build probe must NOT require an admin token — otherwise it
    can't be used as the dashboard's first-contact health check."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from spot_aggro.api import routes as spot_routes

    app = FastAPI()
    app.include_router(spot_routes.router)
    c = TestClient(app)
    # No token header at all.
    r = c.get("/spot_aggro/build")
    assert r.status_code == 200


def test_dashboard_probes_server_build():
    s = _src()
    assert 'fetch(API + "/spot_aggro/build"' in s, (
        "dashboard must probe /spot_aggro/build at load to verify API "
        "parity with the HTML it is serving"
    )
    # Mismatch must flip the pill to bad + actionable tooltip.
    assert "BUILD · MISMATCH" in s
    assert "BUILD · SERVER STALE" in s
    # And the sync case paints it green.
    assert 'pill.className = "build-pill ok"' in s


def test_dashboard_reprobes_build_periodically():
    """Without periodic reprobe, a server restart would not flip the
    pill green until the operator manually reloaded — which is exactly
    the blind-spot that made the 12h-stuck state invisible."""
    s = _src()
    assert "setInterval(refreshBuildPill, 30000)" in s
