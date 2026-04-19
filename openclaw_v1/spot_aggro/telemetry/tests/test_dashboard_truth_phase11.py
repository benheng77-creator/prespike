"""Phase 11 final dashboard truth-coherence fixes — static assertions.

We do not spin up a browser. Instead we treat web/ops/index.html as a
single governed artifact and assert the invariants the operator signed
off on:

    1. MIO age parser accepts "13m ago" / "5s" / "1h 10m" — the
       forms the refresh() path actually writes.
    2. Open Positions renderer uses sentinel-safe labels, not raw
       999%/99.9% TP/SL values, for reconciled modules.
    3. Recent Trade Consensus marks Members<4 rows as PARTIAL.
    4. Executed Trade Activity tags M_reconciled* rows as "RECON".
    5. Decision Funnel has explicit diagnosis labels
       (IDLE WINDOW / NO QUALIFYING CANDIDATES / CONSENSUS SILENT /
        CONSENSUS REJECTING 100% / ORDER PATH BROKEN / 100% REJECT).
    6. Tier Heatmap renders an OFF-ENUM separator for reconciled exits.
    7. Infrastructure Health headline/body are bound to the same `wd`
       payload in the refresh() block (no stale previous-render leak).
    8. System Issues summary never claims "truth layer coherent" when
       watchdog rows are visible.
    9. Symbol Review requires ≥3 exits before asserting strong/weak.
   10. Reconciliation Status surfaces ⚠ on |delta| ≥ 10% and the
       RECONCILIATION_PENDING label is shown as "PENDING".

These are whole-file string assertions — the dashboard is a governed
truth system, and any refactor that loses one of these invariants MUST
fail this test.
"""
from __future__ import annotations

from pathlib import Path
import re


REPO = Path(__file__).resolve().parents[4]
INDEX = REPO / "web" / "ops" / "index.html"


def _src() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_mio_parser_accepts_ago_suffix() -> None:
    """The broken regex was `^(?:Xh)?(?:Xm)?(?:Xs)?$` — rejected '13m ago'."""
    s = _src()
    # New parser uses greedy unit extractors, not anchored full-string.
    assert "ageText.match(/(\\d+)\\s*h/)" in s
    assert "ageText.match(/(\\d+)\\s*m(?!s)/)" in s
    assert "ageText.match(/(\\d+)\\s*s(?!ec|h)/)" in s
    # Regression guard: the old anchored pattern must NOT be present.
    assert "/^(?:(\\d+)h\\s*)?(?:(\\d+)m\\s*)?(?:(\\d+)s)?$/" not in s


def test_open_positions_sentinel_safe_labels() -> None:
    s = _src()
    # Any of these labels must appear verbatim in the positions renderer.
    for label in ("RECONCILED HOLD", "LOW-CONF HOLD", "NO LIVE TP",
                  "NO LIVE SL", "MANUAL / PENDING", "NO TP/SL SET"):
        assert label in s, f"positions label missing: {label}"
    # Sentinel detection threshold present.
    assert "tp >= 50" in s
    assert "sl <= -0.95" in s


def test_consensus_members_partial() -> None:
    s = _src()
    assert "partial = members < 4" in s
    assert "PARTIAL" in s and "NON-BINDING" in s


def test_trade_activity_tags_reconciliation() -> None:
    s = _src()
    assert 'isRecon = mod.startsWith("M_reconciled")' in s
    assert "RECON" in s  # The action pill tag text.
    assert "reconciliation-generated — NOT live strategy" in s


def test_funnel_has_explicit_diagnosis_labels() -> None:
    s = _src()
    for label in ("IDLE WINDOW", "NO QUALIFYING CANDIDATES",
                  "CONSENSUS SILENT", "CONSENSUS REJECTING 100%",
                  "ORDER PATH BROKEN", "100% REJECT",
                  "PARTIAL · engine stopped"):
        assert label in s, f"funnel label missing: {label}"


def test_heatmap_off_enum_separator() -> None:
    """Phase 11b supersedes the inline OFF-ENUM row with a dedicated Lane 3
    table for reconciled activity. The separation is stricter, not weaker:
    canonical A+/A/B/C never mixes with reconciled rows at all."""
    s = _src()
    # Lane 3 section header must exist.
    assert "Lane 3 · Reconciliation activity" in s
    # A dedicated tbody for reconciled rows.
    assert 'id="tb-heatmap-reconciled"' in s
    # Operator still sees the warning that reconciled ≠ canonical.
    assert "NOT canonical tier" in s


def test_infra_headline_body_same_payload() -> None:
    """Headline + body are bound to `wd` in the SAME if/else block."""
    s = _src()
    # New block has a single branch on `wd` that writes BOTH summary and body.
    pattern = re.compile(
        r"if \(!wd\) \{[^}]*infra-summary[^}]*tb-infra.*?"
        r"\} else \{.*?infra-summary.*?tb-infra",
        re.DOTALL,
    )
    assert pattern.search(s), "infra headline+body are not bound together in refresh()"


def test_issues_summary_no_false_coherence() -> None:
    s = _src()
    # New code only prints "truth layer coherent" when allRows.length === 0.
    assert "truth layer coherent (no visible rows)" in s
    # Old unconditional phrasing must be gone.
    assert ": \" · truth layer coherent\"" not in s


def test_symbol_review_requires_sample() -> None:
    s = _src()
    assert "MIN_CONFIDENT_EXITS = 3" in s
    assert "LOW DATA" in s
    assert "insufficient sample" in s


def test_reconciliation_risk_surfacing() -> None:
    s = _src()
    # Delta threshold amplification.
    assert "Math.abs(deltaN) >= 0.10" in s
    assert "rc-delta-big" in s
    # Pending row tint.
    assert "rc-row-pending" in s
    # Visible PENDING label instead of raw RECONCILIATION_PENDING.
    assert 'label = st === "RECONCILIATION_PENDING" ? "PENDING"' in s


def test_runtime_decisions_labeled_session_local() -> None:
    s = _src()
    assert "session-local · resets on restart" in s


def test_ledger_window_basis_corrected() -> None:
    s = _src()
    # Old misleading "24h" basis is replaced.
    assert 'data-window-basis="session-local"' in s
    assert "spot_aggro:trade_log_session" in s


def test_strength_vs_gap_registers_every_refresh() -> None:
    """SvG now calls registerTruthEvidence INSIDE refresh()'s {quadrant} block,
    not only from the 1-min Executive Read — so the card never goes stale
    relative to the 5s refresh cadence."""
    s = _src()
    # Find the quadrant refresh block and make sure it registers evidence.
    m = re.search(
        r"// ── A1\. Strength-vs-Gap Quadrant ──.*?"
        r"registerTruthEvidence\(\"c-quadrant\"",
        s,
        re.DOTALL,
    )
    assert m, "c-quadrant is not re-registered inside refresh()"


def test_card_body_stale_banner_present() -> None:
    """Visible stale/mismatch body banners, not only governor."""
    s = _src()
    assert ".c.card-stale .b::before" in s
    assert ".c.card-mismatch .b::before" in s
    assert "card data stale" in s
    assert "summary numbers do not reconcile" in s
