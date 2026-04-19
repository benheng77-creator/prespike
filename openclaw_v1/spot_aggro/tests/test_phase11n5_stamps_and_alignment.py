"""Phase 11n-5 — Stamp freshness + grid alignment locks.

Root cause of the AGING badges + 'live refresh path not updating this
body' warning: the Phase 11n/11n-3 cards called a phantom function
`registerEvidence(...)` guarded with `&&`, which silently no-ops because
the function doesn't exist. The correct call is `_regEvMinimal(...)`
which plumbs into the `registerTruthEvidence` stamp pipeline.

Root cause of the misaligned rows (half-filled rows with big empty
right side): two governance cards were `class="c span3"` while their
siblings were single-column. Uniformized so every governance card
participates in the 3-column grid consistently.

Locks:
  1. No phantom `registerEvidence && registerEvidence(` calls remain.
  2. Every new card (c-scenarios, c-research-gov, c-cards-gov,
     c-decision, c-auto, c-loop-novelty) calls `_regEvMinimal(cardId,
     ttl, n)` to keep its stamp fresh.
  3. No Phase 11n card carries `span3` — they all participate in the
     standard 3-column grid.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


HTML_PATH = REPO / "web" / "ops" / "index.html"


def _html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def test_no_phantom_register_evidence_calls():
    html = _html()
    bad = re.findall(r"registerEvidence\s*&&\s*registerEvidence\s*\(", html)
    assert not bad, (
        f"found {len(bad)} phantom registerEvidence() guards — these silently "
        "no-op and leave cards AGING. Use _regEvMinimal(cardId, ttl, n)."
    )


def test_every_new_card_calls_reg_ev_minimal():
    # Phase 11n-6: consolidated to c-research + c-gov + c-auto.
    # Each must have at least one _regEvMinimal("<id>", ...) call.
    html = _html()
    expected = ["c-research", "c-gov", "c-auto"]
    missing = []
    for cid in expected:
        pattern = rf'_regEvMinimal\(\s*["\']{re.escape(cid)}["\']'
        if not re.search(pattern, html):
            missing.append(cid)
    assert not missing, (
        f"these cards do not refresh their stamp: {missing}. "
        "Add a _regEvMinimal() call inside their fetchX() handler."
    )


def test_no_phase11n_card_uses_span3():
    html = _html()
    # Phase 11n-6: only the consolidated cards remain as governance row.
    # c-research may use span2 (half-row), others stay single-column.
    # Phase 11n-8: c-auto is legitimately full-row at the BOTTOM of
    # the grid (100%-auto infrastructure), so it's exempt from this
    # alignment rule.
    expected = ["c-research", "c-gov"]
    offenders = []
    for cid in expected:
        m = re.search(rf'<div class="([^"]+)" id="{re.escape(cid)}"', html)
        if not m:
            continue
        cls = m.group(1)
        if "span3" in cls.split():
            offenders.append(f"{cid} (class={cls!r})")
    assert not offenders, (
        "Phase 11n cards must participate in the 3-col grid without span3 "
        f"to keep rows aligned. Offenders: {offenders}"
    )
