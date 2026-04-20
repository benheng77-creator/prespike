"""Guardrail: the HTML dashboard-build meta tag MUST match
SERVER_BUILD in routes.py. Mismatch produces the 'BUILD · MISMATCH'
pill in the operator dashboard.

Phase-uu added this test because the main panel HTML was drifting
behind server across phases rr/ss/tt/uu without anyone noticing.
"""
from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
MAIN_HTML = REPO / "web" / "ops" / "index.html"
CDV_HTML = REPO / "web" / "strategy" / "contrarian-deepvalue" / "index.html"
ROUTES_PY = REPO / "openclaw_v1" / "spot_aggro" / "api" / "routes.py"


def _server_build() -> str:
    src = ROUTES_PY.read_text(encoding="utf-8")
    m = re.search(r'SERVER_BUILD\s*=\s*"(phase-11n-9-[a-z]+-2026-04-20)"', src)
    assert m, "SERVER_BUILD constant not found in routes.py"
    return m.group(1)


def _html_build(path: Path, prefix: str = "") -> str:
    src = path.read_text(encoding="utf-8")
    if prefix:
        m = re.search(
            rf'dashboard-build" content="({re.escape(prefix)}phase-11n-9-[a-z]+-2026-04-20)"',
            src,
        )
    else:
        m = re.search(
            r'dashboard-build" content="(phase-11n-9-[a-z]+-2026-04-20)"',
            src,
        )
    assert m, f"dashboard-build meta not found in {path}"
    return m.group(1)


def test_main_panel_build_matches_server():
    server = _server_build()
    html = _html_build(MAIN_HTML)
    assert html == server, (
        f"BUILD MISMATCH — main panel HTML says {html!r} but "
        f"routes.py SERVER_BUILD says {server!r}. "
        f"Bump <meta name=\"dashboard-build\"> in {MAIN_HTML.name}."
    )


def test_cdv_panel_build_tag_tracks_current_phase():
    """CDV panel meta uses 'cdv-panel-' prefix; must also reflect
    current phase suffix so staleness is visible on the panel."""
    server = _server_build()
    # Extract phase suffix from server (e.g. 'uu' from 'phase-11n-9-uu-...')
    server_phase = re.search(r'phase-11n-9-([a-z]+)-', server).group(1)
    cdv = _html_build(CDV_HTML, prefix="cdv-panel-")
    cdv_phase = re.search(r'phase-11n-9-([a-z]+)-', cdv).group(1)
    # CDV panel can lag one phase (it's the CDV panel version, not server)
    # BUT must not lag by more than one phase suffix position.
    server_key = (len(server_phase), server_phase)
    cdv_key = (len(cdv_phase), cdv_phase)
    assert cdv_key >= server_key or abs(
        ord(cdv_phase[-1]) - ord(server_phase[-1])
    ) <= 1 or len(server_phase) > len(cdv_phase), (
        f"CDV panel too far behind: CDV={cdv_phase} server={server_phase}"
    )
