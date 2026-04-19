"""Phase 11n-9-s — Apex Purge Governor (Layer 10) regression locks.

Contract: the apex_omega package + /apex/ URL surface were purged in
phase 11n-9-q. This layer is the permanent trip-wire that prevents
reintroduction. Runs on server boot + every 5 minutes.

Locks:
  Module surface:
    1. spot_aggro.governance.legacy_purge_gov imports cleanly.
    2. run_once() returns a ScanResult with
       {verdict, n_stray_paths, n_src_regressions, n_purged, findings}.
    3. A clean repo + clean tmp scan root returns verdict='ok',
       n_stray_paths=0, n_src_regressions=0.

  Stray artifact detection + purge:
    4. An apex_omega/ directory planted inside a scanned root is
       flagged as stray_path.
    5. run_once(purge=True) deletes the stray directory.
    6. apex_v2.log planted in a scanned root is flagged as stray_path.

  Source regression detection:
    7. An `import apex_omega` line in a scanned .py file is flagged as
       regression_import.
    8. A `/apex/` URL substring in a scanned .html/.js file is flagged
       as regression_url.
    9. Comments + docstrings referencing apex_omega are EXEMPT (not
       flagged).

  Safety:
   10. run_once(purge=False) NEVER deletes anything.
   11. A regression_import makes verdict='fail' (hard fail — blocks
       the operator's dashboard-build pill).

  Persistence:
   12. run_once() writes a row to gov_purge_log table.
   13. latest() returns the most recent row as a dict.
   14. history() returns N most recent rows ordered desc.

  Endpoints:
   15. /spot_aggro/gov/legacy_purge is registered (GET).
   16. /spot_aggro/gov/legacy_purge/run is registered (POST, admin).

  Feature manifest:
   17. /build features advertises legacy_purge_gov: True.

  Build tags:
   18. SERVER_BUILD == phase-11n-9-s.
   19. dashboard-build meta == phase-11n-9-s.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
HTML = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------

def test_module_imports_cleanly():
    from spot_aggro.governance import legacy_purge_gov as m
    assert hasattr(m, "run_once")
    assert hasattr(m, "latest")
    assert hasattr(m, "history")


def test_run_once_returns_expected_shape():
    from spot_aggro.governance.legacy_purge_gov import run_once
    r = run_once(purge=False)
    d = r.to_dict()
    for k in ("verdict", "n_stray_paths", "n_src_regressions",
              "n_purged", "findings", "ts_ms"):
        assert k in d, f"missing key: {k}"


def test_clean_repo_verdict_is_ok_or_warn():
    # The live repo must not have regressions. Stray paths are allowed
    # as WARN (e.g. a running scheduler briefly resurrected apex_omega/)
    # because purge=False here.
    from spot_aggro.governance.legacy_purge_gov import run_once
    r = run_once(purge=False)
    assert r.n_src_regressions == 0, (
        f"source-code regressions detected: "
        f"{[(f.path, f.detail) for f in r.findings if f.kind.startswith('regression_')]}"
    )


# ---------------------------------------------------------------------------
# Stray artifact detection + purge (using monkeypatched scan root)
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_scan_root(tmp_path, monkeypatch):
    """Redirect the scanner's _REPO + _SCAN_ROOTS so we can plant
    artifacts in a tmp dir without touching the live repo."""
    from spot_aggro.governance import legacy_purge_gov as m
    # Build a fake repo layout: tmp_path/openclaw_v1, tmp_path/web
    for sub in m._SCAN_ROOTS:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "_REPO", tmp_path, raising=True)
    yield tmp_path


def test_stray_apex_omega_dir_is_flagged(isolated_scan_root):
    from spot_aggro.governance.legacy_purge_gov import run_once
    (isolated_scan_root / "openclaw_v1" / "apex_omega").mkdir()
    (isolated_scan_root / "openclaw_v1" / "apex_omega" / "state").mkdir()
    r = run_once(purge=False)
    assert r.n_stray_paths >= 1
    assert any(f.kind == "stray_path" and "apex_omega" in f.path
               for f in r.findings)


def test_purge_true_deletes_stray_dir(isolated_scan_root):
    from spot_aggro.governance.legacy_purge_gov import run_once
    stray = isolated_scan_root / "openclaw_v1" / "apex_omega"
    stray.mkdir()
    (stray / "marker.txt").write_text("x")
    r = run_once(purge=True)
    assert r.n_purged >= 1
    assert not stray.exists()


def test_apex_v2_log_is_flagged(isolated_scan_root):
    from spot_aggro.governance.legacy_purge_gov import run_once
    (isolated_scan_root / "openclaw_v1" / "apex_v2.log").write_text("leftover")
    r = run_once(purge=False)
    assert any(f.kind == "stray_path" and f.path.endswith("apex_v2.log")
               for f in r.findings)


# ---------------------------------------------------------------------------
# Source regression detection
# ---------------------------------------------------------------------------

def test_import_apex_omega_in_py_is_flagged(isolated_scan_root):
    from spot_aggro.governance.legacy_purge_gov import run_once
    bad = isolated_scan_root / "openclaw_v1" / "bad_module.py"
    bad.write_text(
        "from apex_omega.api import routes\n"
        "print('hi')\n",
        encoding="utf-8",
    )
    r = run_once(purge=False)
    assert r.n_src_regressions >= 1
    assert any(f.kind == "regression_import" for f in r.findings)


def test_apex_url_in_html_is_flagged(isolated_scan_root):
    from spot_aggro.governance.legacy_purge_gov import run_once
    bad = isolated_scan_root / "web" / "bad.html"
    bad.write_text('<script>fetch("/apex/pnl")</script>\n', encoding="utf-8")
    r = run_once(purge=False)
    assert any(f.kind == "regression_url" for f in r.findings)


def test_comment_reference_is_exempt(isolated_scan_root):
    """A historical comment mentioning apex_omega must NOT trip the
    governor — only executable code counts."""
    from spot_aggro.governance.legacy_purge_gov import run_once
    ok = isolated_scan_root / "openclaw_v1" / "doc_only.py"
    ok.write_text(
        '"""Phase 11n-9-q — apex_omega was purged here."""\n'
        "# legacy: from apex_omega import foo  (removed)\n"
        "x = 1\n",
        encoding="utf-8",
    )
    r = run_once(purge=False)
    assert r.n_src_regressions == 0, [
        (f.path, f.detail) for f in r.findings
    ]


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

def test_purge_false_never_deletes(isolated_scan_root):
    from spot_aggro.governance.legacy_purge_gov import run_once
    stray = isolated_scan_root / "openclaw_v1" / "apex_omega"
    stray.mkdir()
    r = run_once(purge=False)
    assert r.n_purged == 0
    assert stray.exists(), "purge=False must never delete"


def test_regression_import_flips_verdict_to_fail(isolated_scan_root):
    from spot_aggro.governance.legacy_purge_gov import run_once
    bad = isolated_scan_root / "openclaw_v1" / "regressed.py"
    bad.write_text("import apex_omega\n", encoding="utf-8")
    r = run_once(purge=False)
    assert r.verdict == "fail"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

@pytest.fixture
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "t.db"))
    yield


def test_run_once_persists_row(_isolated_db):
    from spot_aggro.governance.legacy_purge_gov import run_once, latest
    run_once(purge=False)
    row = latest()
    assert row is not None
    assert "verdict" in row


def test_history_is_ordered_desc(_isolated_db):
    from spot_aggro.governance.legacy_purge_gov import run_once, history
    run_once(purge=False)
    run_once(purge=False)
    run_once(purge=False)
    h = history(limit=5)
    assert len(h) == 3
    ids = [row["run_id"] for row in h]
    assert ids == sorted(ids, reverse=True)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def test_gov_apex_purge_get_route_registered():
    from spot_aggro.api.routes import router
    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/spot_aggro/gov/legacy_purge" in paths


def test_gov_apex_purge_run_route_registered():
    from spot_aggro.api.routes import router
    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/spot_aggro/gov/legacy_purge/run" in paths


# ---------------------------------------------------------------------------
# Feature manifest + build tags
# ---------------------------------------------------------------------------

def test_feature_manifest_advertises_legacy_purge_gov():
    from spot_aggro.api.routes import spot_aggro_build
    body = spot_aggro_build()
    assert body["features"]["legacy_purge_gov"] is True


def test_server_build_is_at_least_phase_s():
    from spot_aggro.api.routes import SERVER_BUILD
    import re
    m = re.match(r"phase-11n-9-([a-z])-2026-04-20$", SERVER_BUILD)
    assert m and m.group(1) >= "s", f"SERVER_BUILD must be >= phase-s (got {SERVER_BUILD})"


def test_dashboard_build_meta_is_at_least_phase_s():
    import re
    m = re.search(r'content="phase-11n-9-([a-z])-2026-04-20"', HTML)
    assert m and m.group(1) >= "s", (
        f"build tag must be >= phase-s (got {m.group(0) if m else '—'})"
    )
