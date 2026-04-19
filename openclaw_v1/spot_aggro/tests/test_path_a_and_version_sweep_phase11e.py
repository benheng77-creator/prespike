"""Phase 11e final — Path A (local dashboard mount) + version-wording sweep.

Root-cause context:
  For 12+ hours the operator was viewing https://claw247-trading.pages.dev/ops/
  which served a 52 KB HTML titled "claw247 - operator" that matched no
  branch in the repo. Every card fix we shipped was on disk (web/ops/index.html,
  213 KB, Phase 11d) but never reached the browser. Path A (this file's
  contract) mounts web/ops/ directly through the local uvicorn at /ops/ so
  the operator can view Phase 11d without any Cloudflare deploy round-trip.

  This file also locks the global version-wording sweep: no legacy "v2"
  phrasing in operator-visible text, posture and trust-governor wording
  no longer contradict each other, Coin Accuracy Matrix requires ≥3 exits
  before claiming STRONG/OK/WEAK.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
INDEX = REPO / "web" / "ops" / "index.html"


def _src() -> str:
    return INDEX.read_text(encoding="utf-8")


def _server() -> str:
    return (REPO / "openclaw_v1" / "server.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Path A — local uvicorn serves the dashboard
# ---------------------------------------------------------------------------

def test_server_mounts_web_ops_at_ops_prefix():
    """uvicorn must serve web/ops/ at /ops/ so the operator can reach the
    current disk HTML without depending on Cloudflare Pages deploy state."""
    src = _server()
    assert "StaticFiles" in src, (
        "server.py must import StaticFiles to mount the dashboard"
    )
    assert 'app.mount("/ops"' in src, (
        "dashboard must be mounted at /ops/ prefix"
    )
    assert "html=True" in src, (
        "StaticFiles mount must set html=True so /ops/ resolves to index.html"
    )


def test_server_mount_resolves_to_web_ops():
    """The mount target must be the repo's web/ops directory — not some
    stale deploy dir or a drift copy. Any future refactor that changes the
    target must update this test explicitly."""
    src = _server()
    assert '"web" / "ops"' in src or '/web/ops' in src, (
        "dashboard mount must point at <repo>/web/ops"
    )


def test_server_mount_is_defensive_to_missing_dir():
    """If web/ops/ doesn't exist (fresh checkout, CI), the mount must log
    a warning and NOT raise — server boot can't fail on a cosmetic surface."""
    src = _server()
    assert "_ops_dir.exists()" in src
    assert "dashboard mount failed" in src  # exception path covered


def test_server_logs_mount_location_for_operator_visibility():
    src = _server()
    assert "dashboard mounted at /ops/" in src, (
        "operators must see the mount path in uvicorn logs to confirm "
        "which HTML file is being served"
    )


# ---------------------------------------------------------------------------
# Version-wording sweep — no legacy "v2" phrasing in operator-visible text
# ---------------------------------------------------------------------------

LEGACY_VISIBLE_STRINGS = (
    # These legacy phrases appeared in user-visible locations (innerHTML,
    # textContent assignments, card subtitles, helpers, gate-summary copy).
    # Each one is now replaced with version-neutral operator wording.
    "Trading active — v2 tiered",
    "scored coins · spot_aggro v2",
    '"spot_aggro v2"',  # set-uni textContent — must not appear verbatim
)


@pytest.mark.parametrize("legacy", LEGACY_VISIBLE_STRINGS)
def test_no_legacy_v2_wording_in_operator_visible_text(legacy):
    s = _src()
    assert legacy not in s, (
        f"legacy version wording '{legacy}' still present — "
        f"operator-visible text must read as one current system, "
        f"not a mix of old and new phases"
    )


CURRENT_PHRASINGS = (
    "Trading active — tiered execution",
    "scored coins · tiered universe",
    "tiered universe (A+ · A · B · C)",
)


@pytest.mark.parametrize("phrase", CURRENT_PHRASINGS)
def test_current_version_phrasing_present(phrase):
    """After the sweep, these are the replacement phrasings."""
    s = _src()
    assert phrase in s, f"current-version phrasing missing: {phrase}"


# ---------------------------------------------------------------------------
# Trust governor vs POSTURE — must not contradict each other
# ---------------------------------------------------------------------------

def test_governor_distinguishes_watchdog_from_truth_findings():
    """Previously the governor said 'watchdog has open findings' even when
    the rows in #tb-issues were purely stale-card truth-coherence entries
    (zero watchdog rows). That made POSTURE: READY and DEGRADED TRUST look
    like a contradiction. The fix counts truth vs watchdog rows separately
    and names them honestly."""
    s = _src()
    # Separate counters and labels must exist.
    assert "stale card" in s
    assert "summary/body mismatch" in s
    # The "watchdog has open findings" unconditional message must be gone.
    assert 'reasons.push("watchdog has open findings")' not in s, (
        "governor must not claim 'watchdog findings' when the rows are "
        "truth-coherence findings only — this is the operator-visible "
        "contradiction we are removing"
    )
    # New phrasing variants must appear.
    assert "truth-coherence finding" in s


def test_posture_ready_mentions_trust_governor_as_separate_surface():
    """When posture is READY and no watchdog findings, the detail must
    explicitly tell the operator that the page-level trust governor is a
    separate check — so a DEGRADED TRUST banner above a READY posture
    does not look contradictory."""
    s = _src()
    assert "trust governor is a separate truth-layer check" in s


# ---------------------------------------------------------------------------
# Open Positions — column headers match content reality
# ---------------------------------------------------------------------------

def test_open_positions_column_header_is_content_agnostic():
    """The TP column header used to say 'TP %' — misleading when the cell
    content is a pill label like 'RECONCILED HOLD' rather than a percent.
    New headers are generic: TP / TP price / SL price / Max hold / Module."""
    s = _src()
    # Old specific headers must be gone.
    assert '<th class="r">TP %</th>' not in s
    assert '<th class="r">Sell at (TP)</th>' not in s
    # New generic headers must be present.
    for header in ("<th class=\"r\">TP</th>",
                   "<th class=\"r\">TP price</th>",
                   "<th class=\"r\">SL price</th>",
                   "<th class=\"r\">Max hold</th>",
                   "<th>Module</th>"):
        assert header in s, f"positions column header missing: {header}"


# ---------------------------------------------------------------------------
# Coin Accuracy Matrix — LOW DATA must precede STRONG/OK/WEAK
# ---------------------------------------------------------------------------

def test_matrix_verdict_low_data_is_evaluated_before_strong_ok_weak():
    """A 1-exit win could stamp 'STRONG' under the old ordering. The fix
    puts LOW DATA (s.exits < 3) first in the verdict ladder so confidence
    wording can never exceed sample size."""
    s = _src()
    # Look for the new ladder ordering in the tb-matrix renderer.
    # The new code uses explicit if/else branches, not a ternary chain.
    # Anchor on the if/else cascade itself, not on the explanatory comment
    # above it (which mentions STRONG in prose and threw off the earlier
    # index comparison). The cascade starts with `if (s.exits === 0)`.
    anchor = 'if (s.exits === 0)'
    idx = s.find(anchor)
    assert idx > 0, "matrix verdict if/else cascade missing"
    end = s.find('</tr>`;', idx)
    assert end > 0, "could not locate end of matrix row template"
    window = s[idx: end]
    low_idx = window.find('"LOW DATA"')
    strong_idx = window.find('"STRONG"')
    assert low_idx > 0 and strong_idx > 0, (
        f"LOW_DATA or STRONG missing in cascade window "
        f"(low_idx={low_idx}, strong_idx={strong_idx})"
    )
    assert low_idx < strong_idx, (
        "LOW DATA verdict check must precede STRONG in the matrix ladder "
        f"(low_idx={low_idx}, strong_idx={strong_idx})"
    )


def test_matrix_meta_reports_confident_sample_count():
    s = _src()
    assert "with ≥3 closed exits" in s, (
        "matrix-meta must surface how many coins have confident sample"
    )
