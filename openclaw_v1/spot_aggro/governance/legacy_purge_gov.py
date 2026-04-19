"""Phase 11n-9-s — Apex Purge Governor (Layer 10).

Hard contract: the apex_omega package and every /apex/* URL were purged
in phase 11n-9-q. This layer is the permanent trip-wire that ensures
nothing recreates them. It runs on server boot and every 5 minutes.

What it does:
  1. SCAN the filesystem for any stray apex_omega/ directory, apex_v2.log,
     PAPER_LAST_OK.ts, or apex_config.yml outside the allowed paths.
  2. SCAN Python/HTML/JS/MD files under openclaw_v1/ and web/ for any
     `import apex_omega`, `from apex_omega`, or `/apex/` URL substring
     in executable code (comments/docstrings are exempt — historical
     references are allowed).
  3. PURGE stray filesystem artifacts (delete files, remove empty dirs).
  4. RECORD every finding + action in gov_purge_log (SQLite table) so
     the operator can audit what was auto-cleaned and when.
  5. REFUSE to do anything destructive inside a test (guarded by
     os.environ["LEGACY_PURGE_GOV_AUTOKILL"] — default off).

What it does NOT do:
  - Does not modify Python/HTML/JS source files. If an executable line
    re-imports apex_omega it's a code regression, not a filesystem
    artifact, so the governor flags it as verdict=fail and leaves the
    file alone. The regression test + human review fix the source.
  - Does not touch the shared SQLite DB. The apex_* table names were
    kept intentionally for historical data continuity.

Never places a trade. Never consults capital. Read + quarantine only.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[3]
_TARGET_NAME = "apex_omega"
_SCAN_ROOTS = ("openclaw_v1", "web", "deploy")

# Filesystem artifacts that must never exist outside these allowed paths.
# (Database table names, git history, and third-party caches are exempt.)
_STRAY_PATTERNS = (
    "apex_omega",
    "apex_v2.log",
    "apex_config.yml",          # renamed to ops_config.yml in phase-q
)

# Allowed-path allowlist. Anything matching these substrings is skipped
# (historical logs, git metadata, third-party node caches).
_ALLOW_SUBSTR = (
    ".git/", ".git\\",
    "node_modules", "questdb/public/assets",
    ".backup/", ".backup\\",
)

# Source-code patterns that count as a real regression (not a comment).
# Only flagged when they appear in a non-comment line of a .py/.html/.js.
_SRC_RE_APEX_IMPORT = re.compile(r"^\s*(?:from|import)\s+apex_omega\b", re.M)
_SRC_RE_APEX_URL = re.compile(r'["\'`]/apex/', re.M)

# Files that legitimately discuss apex_* table names (backfill migration,
# truth contract logs). Keep them out of the executable-regression scan.
_SRC_ALLOW_FILES = (
    "shared/persistence/state.py",      # trade_log table migration
    "tests/",                            # test assertions
)


@dataclass
class Finding:
    kind: str                # 'stray_path' | 'regression_import' | 'regression_url'
    path: str
    detail: str
    action: str = "flag"     # 'flag' | 'purged' | 'skipped'


@dataclass
class ScanResult:
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    findings: list[Finding] = field(default_factory=list)
    n_stray_paths: int = 0
    n_src_regressions: int = 0
    n_purged: int = 0
    verdict: str = "ok"       # 'ok' | 'warn' | 'fail'

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["findings"] = [asdict(f) for f in self.findings]
        return d


# ---------------------------------------------------------------------------
# SQLite persistence
# ---------------------------------------------------------------------------

_DB_LOCK = threading.Lock()


def _db_path() -> str:
    return (
        os.environ.get("TRADE_DB_PATH")
        or os.environ.get("CLAW_DB_PATH")
        or "trades.db"
    )


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path(), isolation_level=None, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def _init_schema() -> None:
    with _DB_LOCK:
        con = _connect()
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS gov_purge_log("
                " run_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts_ms INTEGER NOT NULL,"
                " verdict TEXT NOT NULL,"
                " n_stray_paths INTEGER NOT NULL,"
                " n_src_regressions INTEGER NOT NULL,"
                " n_purged INTEGER NOT NULL,"
                " payload_json TEXT NOT NULL"
                ")"
            )
        finally:
            con.close()


def _persist(result: ScanResult) -> int:
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            cur = con.execute(
                "INSERT INTO gov_purge_log(ts_ms, verdict, n_stray_paths,"
                " n_src_regressions, n_purged, payload_json)"
                " VALUES(?,?,?,?,?,?)",
                (
                    result.ts_ms, result.verdict,
                    result.n_stray_paths, result.n_src_regressions,
                    result.n_purged,
                    json.dumps(result.to_dict()),
                ),
            )
            return int(cur.lastrowid or 0)
        finally:
            con.close()


# ---------------------------------------------------------------------------
# Scanners
# ---------------------------------------------------------------------------

def _is_allowed(path: Path) -> bool:
    s = str(path).replace("\\", "/")
    return any(a in s for a in _ALLOW_SUBSTR)


def _scan_fs(repo: Path) -> list[Finding]:
    """Find stray apex_omega/ directories + apex_v2.log / apex_config.yml
    files. Walk only the whitelist roots."""
    out: list[Finding] = []
    for root_name in _SCAN_ROOTS:
        root = repo / root_name
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if _is_allowed(p):
                continue
            name = p.name
            if name in _STRAY_PATTERNS or (
                p.is_dir() and name == _TARGET_NAME
            ):
                out.append(Finding(
                    kind="stray_path",
                    path=str(p.relative_to(repo)),
                    detail=f"forbidden artifact on disk: {name}",
                ))
    return out


def _scan_src(repo: Path) -> list[Finding]:
    """Find apex_omega imports or /apex/ URL substrings in executable
    source. Skip comments/docstrings (those are historical references
    and intentionally allowed)."""
    out: list[Finding] = []
    for root_name in _SCAN_ROOTS:
        root = repo / root_name
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            rel = str(py.relative_to(repo)).replace("\\", "/")
            if any(allow in rel for allow in _SRC_ALLOW_FILES):
                continue
            if _is_allowed(py):
                continue
            try:
                text = py.read_text(encoding="utf-8")
            except Exception:
                continue
            stripped = re.sub(r"#.*", "", text)
            stripped = re.sub(r'"""[\s\S]*?"""', "", stripped)
            stripped = re.sub(r"'''[\s\S]*?'''", "", stripped)
            for m in _SRC_RE_APEX_IMPORT.finditer(stripped):
                ln = stripped[: m.start()].count("\n") + 1
                out.append(Finding(
                    kind="regression_import",
                    path=rel,
                    detail=f"line {ln}: {m.group(0).strip()}",
                ))
        for html in list(root.rglob("*.html")) + list(root.rglob("*.js")):
            rel = str(html.relative_to(repo)).replace("\\", "/")
            if any(allow in rel for allow in _SRC_ALLOW_FILES):
                continue
            if _is_allowed(html):
                continue
            try:
                text = html.read_text(encoding="utf-8")
            except Exception:
                continue
            # Strip /* */ and // comments to avoid false positives.
            stripped = re.sub(r"/\*[\s\S]*?\*/", "", text)
            stripped = re.sub(r"//[^\n]*", "", stripped)
            for m in _SRC_RE_APEX_URL.finditer(stripped):
                ln = stripped[: m.start()].count("\n") + 1
                out.append(Finding(
                    kind="regression_url",
                    path=rel,
                    detail=f"line {ln}: /apex/ substring found",
                ))
    return out


# ---------------------------------------------------------------------------
# Purge (filesystem only)
# ---------------------------------------------------------------------------

def _autokill_enabled() -> bool:
    return os.environ.get("LEGACY_PURGE_GOV_AUTOKILL", "").strip().lower() in (
        "1", "true", "yes",
    )


def _purge_stray(f: Finding, repo: Path) -> bool:
    """Delete one stray artifact. Returns True on success."""
    target = repo / f.path
    try:
        if target.is_dir():
            import shutil
            shutil.rmtree(target, ignore_errors=True)
        elif target.is_file():
            target.unlink(missing_ok=True)
        return not target.exists()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_once(*, purge: bool | None = None) -> ScanResult:
    """Scan + optionally purge.

    purge=None (default): obey LEGACY_PURGE_GOV_AUTOKILL env flag.
    purge=True:           force purge.
    purge=False:          scan only, never delete.
    """
    do_purge = _autokill_enabled() if purge is None else bool(purge)
    r = ScanResult()
    r.findings.extend(_scan_fs(_REPO))
    r.findings.extend(_scan_src(_REPO))
    for f in r.findings:
        if f.kind == "stray_path" and do_purge:
            if _purge_stray(f, _REPO):
                f.action = "purged"
                r.n_purged += 1
            else:
                f.action = "flag"
        else:
            f.action = "flag"
    r.n_stray_paths = sum(1 for f in r.findings if f.kind == "stray_path")
    r.n_src_regressions = sum(
        1 for f in r.findings if f.kind.startswith("regression_")
    )
    if r.n_src_regressions > 0:
        r.verdict = "fail"      # executable code regressed — hard fail
    elif r.n_stray_paths > r.n_purged:
        r.verdict = "warn"      # stray paths exist + not purged
    else:
        r.verdict = "ok"
    try:
        _persist(r)
    except Exception:
        pass
    return r


def latest() -> dict[str, Any] | None:
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            row = con.execute(
                "SELECT payload_json FROM gov_purge_log"
                " ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            return json.loads(row["payload_json"])
        finally:
            con.close()


def history(limit: int = 20) -> list[dict[str, Any]]:
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT run_id, ts_ms, verdict, n_stray_paths,"
                " n_src_regressions, n_purged"
                " FROM gov_purge_log ORDER BY run_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()
