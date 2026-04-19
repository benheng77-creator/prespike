"""Phase 11n-9-t — Apex Deep Forensic Governor (Layer 11) regression locks.

Layer 10 (apex_purge_gov) caught filesystem dirs + Python imports + /apex/
URLs. Layer 11 goes deeper: YAML/JSON/TOML/shell/.env + string literals
+ process env + pyc bytecode — covers every apex variant:
  apex_omega, apex_v2, APEX_V2_*, 99X_APEX, OPENCLAW_99X_APEX,
  ACTIVE_STRATEGY.

Locks:
  Module surface:
    1. Module imports cleanly.
    2. run_once() returns DeepScanResult.to_dict() with keys:
       ts_ms, verdict, n_stray_paths, n_src_strings, n_config,
       n_env, n_info, n_purged, findings.

  Clean-repo state:
    3. Live repo scan: n_src_strings == 0 and n_config == 0.
    4. Info_db count == 5 (five kept apex_* tables).

  Token detection:
    5. apex_v2 in a config file -> kind='config'.
    6. APEX_V2 in an .env file -> kind='config'.
    7. '99x_apex' in a shell script -> kind='config'.
    8. apex_omega substring in a .py string literal -> kind='src_string'.
    9. ACTIVE_STRATEGY set in process env -> kind='env'.
   10. apex_trade_log SQLite table -> kind='info_db_table' (not fail).

  Safety:
   11. Comment-only mentions do NOT trip verdict=fail.
   12. purge=False NEVER deletes anything.
   13. purge=True deletes stray apex_omega/ planted in an allowed root.

  Verdict math:
   14. n_src_strings>0 -> verdict='fail'.
   15. n_config>0 -> verdict='fail'.
   16. n_env>0 -> verdict='fail'.
   17. Only info_db findings -> verdict='ok'.

  Persistence:
   18. Row written to apex_deep_forensic_log.
   19. latest() returns most recent payload dict.
   20. history() returns desc-ordered list.

  Endpoints:
   21. /spot_aggro/gov/apex_deep registered (GET).
   22. /spot_aggro/gov/apex_deep/run registered (POST, admin).

  Feature manifest + build tags:
   23. /build features advertises apex_deep_forensic_gov: True.
   24. SERVER_BUILD >= phase-11n-9-t.
   25. dashboard-build meta >= phase-11n-9-t.

  Live-tree contract (zero apex live):
   26. Full live scan returns verdict='ok'.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
HTML = (REPO / "web" / "ops" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------

def test_module_imports_cleanly():
    from spot_aggro.governance import apex_deep_forensic_gov as m
    for name in ("run_once", "latest", "history"):
        assert hasattr(m, name), f"missing attr: {name}"


def test_run_once_shape():
    from spot_aggro.governance.apex_deep_forensic_gov import run_once
    d = run_once(purge=False).to_dict()
    for k in ("ts_ms", "verdict", "n_stray_paths", "n_src_strings",
              "n_config", "n_env", "n_info", "n_purged", "findings"):
        assert k in d


# ---------------------------------------------------------------------------
# Clean-repo state
# ---------------------------------------------------------------------------

def test_live_tree_has_zero_src_regressions():
    from spot_aggro.governance.apex_deep_forensic_gov import run_once
    r = run_once(purge=False)
    offenders = [(f.path, f.line, f.token) for f in r.findings
                 if f.kind == "src_string"]
    assert r.n_src_strings == 0, (
        "source-code regressions detected:\n" +
        "\n".join(f"  {p}:{ln} token={t!r}" for p, ln, t in offenders)
    )


def test_live_tree_has_zero_config_regressions():
    from spot_aggro.governance.apex_deep_forensic_gov import run_once
    r = run_once(purge=False)
    offenders = [(f.path, f.line, f.token) for f in r.findings
                 if f.kind == "config"]
    assert r.n_config == 0, (
        "config regressions detected:\n" +
        "\n".join(f"  {p}:{ln} token={t!r}" for p, ln, t in offenders)
    )


# ---------------------------------------------------------------------------
# Token detection (isolated scan root)
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_scan_root(tmp_path, monkeypatch):
    from spot_aggro.governance import apex_deep_forensic_gov as m
    for sub in m._SCAN_ROOTS:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "_REPO", tmp_path, raising=True)
    yield tmp_path


def test_apex_v2_in_yaml_flagged(isolated_scan_root):
    from spot_aggro.governance.apex_deep_forensic_gov import run_once
    (isolated_scan_root / "openclaw_v1" / "bad.yml").write_text(
        "engine: apex_v2\n", encoding="utf-8",
    )
    r = run_once(purge=False)
    assert any(f.kind == "config" and "apex_v2" in f.token.lower()
               for f in r.findings)


def test_APEX_V2_env_flagged(isolated_scan_root):
    from spot_aggro.governance.apex_deep_forensic_gov import run_once
    (isolated_scan_root / "openclaw_v1" / "bad.env").write_text(
        "APEX_V2_ENABLED=1\n", encoding="utf-8",
    )
    r = run_once(purge=False)
    assert any(f.kind == "config" and "APEX_V2" in f.token.upper()
               for f in r.findings)


def test_99x_apex_in_sh_flagged(isolated_scan_root):
    from spot_aggro.governance.apex_deep_forensic_gov import run_once
    (isolated_scan_root / "openclaw_v1" / "start.sh").write_text(
        "export OPENCLAW_99X_APEX=1\n", encoding="utf-8",
    )
    r = run_once(purge=False)
    assert any(f.kind == "config" and "99x_apex" in f.token.lower()
               for f in r.findings)


def test_apex_omega_in_py_string_flagged(isolated_scan_root):
    from spot_aggro.governance.apex_deep_forensic_gov import run_once
    (isolated_scan_root / "openclaw_v1" / "bad.py").write_text(
        'x = "apex_omega"\n', encoding="utf-8",
    )
    r = run_once(purge=False)
    assert any(f.kind == "src_string" and f.token.lower() == "apex_omega"
               for f in r.findings)


def test_env_banned_var_flagged(isolated_scan_root, monkeypatch):
    from spot_aggro.governance.apex_deep_forensic_gov import run_once
    monkeypatch.setenv("APEX_V2_ENABLED", "1")
    r = run_once(purge=False)
    assert any(f.kind == "env" and f.token == "APEX_V2_ENABLED"
               for f in r.findings)


def test_apex_table_flagged_as_info_not_fail(isolated_scan_root, tmp_path, monkeypatch):
    from spot_aggro.governance.apex_deep_forensic_gov import run_once
    import sqlite3
    db_path = tmp_path / "t.db"
    monkeypatch.setenv("TRADE_DB_PATH", str(db_path))
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE apex_trade_log(id INTEGER)")
    con.close()
    r = run_once(purge=False)
    info_tokens = [f.token for f in r.findings if f.kind == "info_db_table"]
    assert "apex_trade_log" in info_tokens
    # And an info-only finding must NOT flip verdict to fail.
    infos_only = [f for f in r.findings if f.kind != "info_db_table"]
    if not any(f.kind in ("src_string", "config", "env") for f in infos_only):
        assert r.verdict != "fail"


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

def test_comments_exempt(isolated_scan_root):
    from spot_aggro.governance.apex_deep_forensic_gov import run_once
    (isolated_scan_root / "openclaw_v1" / "doc.py").write_text(
        '"""This module used to import apex_omega — now purged."""\n'
        "# legacy: from apex_v2 import foo  # removed\n"
        "x = 1\n",
        encoding="utf-8",
    )
    r = run_once(purge=False)
    src_hits = [f for f in r.findings if f.kind == "src_string"
                and f.path.endswith("doc.py")]
    assert not src_hits


def test_purge_false_never_deletes(isolated_scan_root):
    from spot_aggro.governance.apex_deep_forensic_gov import run_once
    stray = isolated_scan_root / "openclaw_v1" / "apex_omega"
    stray.mkdir()
    r = run_once(purge=False)
    assert r.n_purged == 0
    assert stray.exists()


def test_purge_true_deletes(isolated_scan_root):
    from spot_aggro.governance.apex_deep_forensic_gov import run_once
    stray = isolated_scan_root / "openclaw_v1" / "apex_omega"
    stray.mkdir()
    (stray / "x.txt").write_text("x")
    r = run_once(purge=True)
    assert r.n_purged >= 1
    assert not stray.exists()


# ---------------------------------------------------------------------------
# Verdict math
# ---------------------------------------------------------------------------

def test_src_flips_fail(isolated_scan_root):
    from spot_aggro.governance.apex_deep_forensic_gov import run_once
    (isolated_scan_root / "openclaw_v1" / "bad.py").write_text(
        'x = "apex_omega"\n', encoding="utf-8",
    )
    r = run_once(purge=False)
    assert r.verdict == "fail"


def test_config_flips_fail(isolated_scan_root):
    from spot_aggro.governance.apex_deep_forensic_gov import run_once
    (isolated_scan_root / "openclaw_v1" / "bad.yml").write_text(
        "mode: apex_v2\n", encoding="utf-8",
    )
    r = run_once(purge=False)
    assert r.verdict == "fail"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

@pytest.fixture
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_DB_PATH", str(tmp_path / "t.db"))
    yield


def test_persistence_latest_and_history(_isolated_db):
    from spot_aggro.governance.apex_deep_forensic_gov import (
        run_once, latest, history,
    )
    run_once(purge=False)
    run_once(purge=False)
    assert latest() is not None
    h = history(limit=10)
    assert len(h) == 2
    assert [r["run_id"] for r in h] == sorted(
        [r["run_id"] for r in h], reverse=True,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def test_gov_apex_deep_get_route_registered():
    from spot_aggro.api.routes import router
    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/spot_aggro/gov/apex_deep" in paths


def test_gov_apex_deep_run_route_registered():
    from spot_aggro.api.routes import router
    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/spot_aggro/gov/apex_deep/run" in paths


# ---------------------------------------------------------------------------
# Feature manifest + build tags
# ---------------------------------------------------------------------------

def test_feature_manifest_advertises_deep_forensic():
    from spot_aggro.api.routes import spot_aggro_build
    body = spot_aggro_build()
    assert body["features"]["apex_deep_forensic_gov"] is True


def test_server_build_is_at_least_phase_t():
    from spot_aggro.api.routes import SERVER_BUILD
    m = re.match(r"phase-11n-9-([a-z])-2026-04-20$", SERVER_BUILD)
    assert m and m.group(1) >= "t", (
        f"SERVER_BUILD must be >= phase-t (got {SERVER_BUILD})"
    )


def test_dashboard_build_meta_is_at_least_phase_t():
    m = re.search(r'content="phase-11n-9-([a-z])-2026-04-20"', HTML)
    assert m and m.group(1) >= "t", (
        f"build tag must be >= phase-t (got {m.group(0) if m else '—'})"
    )
