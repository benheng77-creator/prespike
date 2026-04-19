"""Phase 11n-9-p — Tab-aware browser audit regression locks.

Before this phase, the browser-side `auditCard()` ran all 6 rules against
every card in the DOM. The tab router hides inactive-tab cards with
`display:none`, so their `_regEvMinimal` never fires, their tbody is
empty, and their data ages past TTL — producing false FAIL/WARN on
17+ cards while the server-side `card_truth_gov` reported 17/17 OK.

Fix: short-circuit `auditCard()` to verdict=`skip` when the card is
hidden (display:none / visibility:hidden / offsetParent null).

Locks:
  auditCard SKIP gate:
    1. auditCard computes `isHidden` via offsetParent + computed style.
    2. Hidden cards return verdict=`skip` and skip all 6 rules.
    3. The skip note calls it out as "hidden (inactive tab)" so the
       operator doesn't mistake it for a real failure.

  auditAllCards counter:
    4. A separate `skip` counter is incremented; skip does NOT fall into
       `fail`.
    5. Console log prints `N ok · N warn · N fail · N skip (N active / N total)`.
    6. paintCardAuditOverview is called with 5 positional args (ids, ok,
       warn, fail, skip).

  paintCardAuditOverview rollup:
    7. The rollup string includes a `skip` segment in grey (#8b949e).
    8. The denominator reads "N active of N cards", not "N cards".
    9. The sort `order` map includes `skip: 3` so skipped tiles sort to
       the end (after ok).

  paintAuditStamps pill:
   10. Skip verdicts map to class `audit-skip`, not `audit-fail`.
   11. Skip pills render as "AUDIT · SKIP" (no age text, which is
       meaningless for an out-of-scope card).

  CSS:
   12. `.cardaudit-cell.skip` rule exists with muted/grey styling.
   13. `.c-stamp.audit-skip` rule exists with muted/grey styling.

  Build tags:
   14. dashboard-build meta reads `phase-11n-9-p-2026-04-20`.
   15. SERVER_BUILD reads `phase-11n-9-p-2026-04-20`.
   16. /build features manifest advertises `tab_aware_audit: True`.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HTML = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# auditCard SKIP gate
# ---------------------------------------------------------------------------

def test_audit_card_has_hidden_check():
    # The gate must use offsetParent (the cheapest hidden-check) + a
    # computed-style check for display/visibility.
    assert "card.offsetParent === null" in HTML
    assert 'style.display === "none"' in HTML
    assert 'style.visibility === "hidden"' in HTML


def test_audit_card_returns_skip_verdict():
    # The short-circuit must return verdict=skip, not some undefined string.
    assert 'verdict: "skip"' in HTML


def test_audit_card_skip_note_is_explanatory():
    # The note must make it clear to the operator this isn't a failure.
    assert "card hidden (inactive tab)" in HTML


# ---------------------------------------------------------------------------
# auditAllCards counter
# ---------------------------------------------------------------------------

def test_audit_all_cards_counts_skip_separately():
    # The ok/warn/fail/skip declaration must include skip.
    assert "let ok = 0, warn = 0, fail = 0, skip = 0" in HTML
    # And skip must be incremented when verdict === "skip".
    assert 'else if (r.verdict === "skip") skip++' in HTML


def test_audit_all_cards_console_log_shows_skip():
    # The console message must include the skip count so devtools shows
    # the correct breakdown.
    assert "skip (${active} active / ${ids.length} total)" in HTML


def test_paint_card_audit_overview_receives_skip():
    # paintCardAuditOverview must be called with the skip count.
    assert "paintCardAuditOverview(ids, ok, warn, fail, skip)" in HTML


# ---------------------------------------------------------------------------
# paintCardAuditOverview rollup + sort
# ---------------------------------------------------------------------------

def test_overview_rollup_includes_skip_segment():
    # The rollup HTML must include a grey skip count segment.
    assert '<span style="color:#8b949e"><b>${sk}</b> skip</span>' in HTML


def test_overview_rollup_uses_active_denominator():
    # The "N of M" phrasing must be "active of cards", not "cards" alone.
    assert "${active} active of ${total} cards" in HTML


def test_overview_sort_order_includes_skip():
    # The sort map must have skip at the end so grey tiles don't
    # interleave with ok tiles.
    assert "const order = { fail: 0, warn: 1, ok: 2, skip: 3 }" in HTML


# ---------------------------------------------------------------------------
# paintAuditStamps pill
# ---------------------------------------------------------------------------

def test_audit_stamp_skip_maps_to_audit_skip_class():
    # The pill-class switch must route skip to audit-skip, not fall
    # through to the default audit-fail.
    assert 'else if (a.verdict === "skip") cls = "audit-skip"' in HTML


def test_audit_stamp_skip_label_is_plain():
    # Skip pills don't show age — they just say "AUDIT · SKIP".
    assert '"AUDIT · SKIP"' in HTML


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

def test_css_cardaudit_cell_skip_defined():
    assert ".cardaudit-cell.skip" in HTML


def test_css_stamp_audit_skip_defined():
    assert ".c-stamp.audit-skip" in HTML


# ---------------------------------------------------------------------------
# Build tags
# ---------------------------------------------------------------------------

def test_dashboard_build_meta_is_at_least_phase_11n_9_p():
    import re
    m = re.search(r'content="phase-11n-9-([a-z])-2026-04-20"', HTML)
    assert m and m.group(1) >= "p", (
        f"build tag must be >= phase-p (got {m.group(0) if m else '—'})"
    )


def test_server_build_is_at_least_phase_11n_9_p():
    from spot_aggro.api.routes import SERVER_BUILD
    import re
    m = re.match(r"phase-11n-9-([a-z])-2026-04-20$", SERVER_BUILD)
    assert m and m.group(1) >= "p", f"SERVER_BUILD must be >= phase-p (got {SERVER_BUILD})"


def test_build_features_advertises_tab_aware_audit():
    from spot_aggro.api.routes import spot_aggro_build
    body = spot_aggro_build()
    assert body["features"]["tab_aware_audit"] is True
