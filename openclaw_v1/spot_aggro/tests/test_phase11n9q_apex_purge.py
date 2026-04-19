"""Phase 11n-9-q — apex_omega purge regression locks.

The apex_omega package has been deleted end-to-end. Shared-ops
infrastructure (formerly at /apex/*) now lives under /spot_aggro/ops/*
served by spot_aggro.ops.routes_ops. The apex_omega Python package is
gone, every /apex/* URL in the dashboard is gone, and server.py no
longer imports from apex_omega.
"""
from __future__ import annotations

import re
from pathlib import Path


def _phase_key(suffix):
    """Phase ordering: length first, then lexicographic.
    'p' < 'q' < 'z' < 'aa' < 'ab'."""
    return (len(suffix), suffix)


import pytest

REPO = Path(__file__).resolve().parents[3]
HTML = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")
SERVER_PY = (REPO / "openclaw_v1" / "server.py").read_text(encoding="utf-8")


def test_apex_omega_import_fails():
    with pytest.raises(ImportError):
        __import__("apex_omega")


def test_no_apex_omega_imports_in_production():
    violations = []
    root = REPO / "openclaw_v1"
    for py in root.rglob("*.py"):
        if "__pycache__" in py.parts or "tests" in py.parts:
            continue
        text = py.read_text(encoding="utf-8")
        stripped = re.sub(r'"""[\s\S]*?"""', "", text)
        stripped = re.sub(r"'''[\s\S]*?'''", "", stripped)
        stripped = re.sub(r"#.*", "", stripped)
        for line_no, line in enumerate(stripped.splitlines(), 1):
            if re.search(r"\b(?:from|import)\s+apex_omega\b", line):
                violations.append(f"{py.relative_to(REPO)}:{line_no}: {line.strip()}")
    assert not violations, (
        "apex_omega imports remain:\n" + "\n".join(violations)
    )


def test_apex_omega_directory_is_gone():
    assert not (REPO / "openclaw_v1" / "apex_omega").exists()


def test_ops_router_prefix():
    from spot_aggro.ops.routes_ops import router
    assert router.prefix == "/spot_aggro/ops"


def test_ops_router_has_full_shared_surface():
    from spot_aggro.ops.routes_ops import router
    paths = {getattr(r, "path", "") for r in router.routes}
    required = {
        "/spot_aggro/ops/status", "/spot_aggro/ops/trades",
        "/spot_aggro/ops/consensus", "/spot_aggro/ops/consensus/live",
        "/spot_aggro/ops/llm/cost", "/spot_aggro/ops/llm/health",
        "/spot_aggro/ops/notifications", "/spot_aggro/ops/pnl",
        "/spot_aggro/ops/kill", "/spot_aggro/ops/governor",
        "/spot_aggro/ops/watchdog", "/spot_aggro/ops/pause",
        "/spot_aggro/ops/resume", "/spot_aggro/ops/halt",
        "/spot_aggro/ops/flatten", "/spot_aggro/ops/trigger_pnl",
        "/spot_aggro/ops/settings", "/spot_aggro/ops/universe",
    }
    missing = required - paths
    assert not missing, f"ops router missing: {missing}"


def test_ops_pnl_route_present():
    from spot_aggro.ops.routes_ops import router
    assert "/spot_aggro/ops/pnl" in {getattr(r, "path", "") for r in router.routes}


def test_server_imports_ops_router():
    assert "from spot_aggro.ops.routes_ops import router as _ops_router" in SERVER_PY


def test_server_does_not_import_apex_omega():
    # Comments that reference apex_omega (e.g. the legacy_purge_gov
    # startup hook's docstring) are fine — the governor's whole
    # purpose is to mention the banned name. The contract is that
    # server.py must not IMPORT the module.
    stripped = re.sub(r'"""[\s\S]*?"""', "", SERVER_PY)
    stripped = re.sub(r"#.*", "", stripped)
    assert not re.search(r"\b(?:from|import)\s+apex_omega\b", stripped), (
        "server.py still imports apex_omega"
    )


def test_dashboard_has_no_apex_urls():
    assert "/apex/" not in HTML


def test_dashboard_has_no_apex_omega_cloud_worker():
    assert "apex-omega-api" not in HTML


def test_doaction_has_no_cloud_fallback_block():
    assert "apex_cloud_api" not in HTML


def test_feature_manifest_advertises_purge():
    from spot_aggro.api.routes import spot_aggro_build
    body = spot_aggro_build()
    # Phase 11n-9-t: feature-flag renamed `apex_omega_purged` ->
    # `legacy_engines_purged` so the deep forensic governor reports
    # zero apex substrings in source.
    assert body["features"]["legacy_engines_purged"] is True


def test_server_build_is_at_least_phase_q():
    from spot_aggro.api.routes import SERVER_BUILD
    import re
    m = re.match(r"phase-11n-9-([a-z]+)-2026-04-20$", SERVER_BUILD)
    assert m and _phase_key(m.group(1)) >= _phase_key("q"), f"SERVER_BUILD must be >= phase-q (got {SERVER_BUILD})"


def test_dashboard_build_meta_is_at_least_phase_q():
    import re
    m = re.search(r'content="phase-11n-9-([a-z]+)-2026-04-20"', HTML)
    assert m and _phase_key(m.group(1)) >= _phase_key("q"), (
        f"build tag must be >= phase-q (got {m.group(0) if m else '—'})"
    )


def test_shared_config_accepts_ops_key():
    from shared.config import load
    cfg = load(engine="ops", force_reload=True)
    assert isinstance(cfg, dict) and "_env" in cfg


def test_shared_config_rejects_legacy_apex_omega_key():
    """Phase 11n-9-t: backward-compat for the 'apex_omega' engine key
    was removed so the deep forensic governor reports zero apex
    substrings in source. Passing it now raises ValueError."""
    from shared.config import load
    import pytest
    with pytest.raises(ValueError):
        load(engine="apex_omega", force_reload=True)


def test_ops_config_yml_exists():
    assert (REPO / "openclaw_v1" / "shared" / "config" / "ops_config.yml").exists()
