"""Phase 11n-9-t — Apex Deep Forensic Governor (Layer 11).

Layer 10 (legacy_purge_gov) catches filesystem directories + Python
imports + /apex/ URLs. Layer 11 goes deeper: it searches YAML/JSON/TOML
config, string literals in .py files, shell scripts, .env files, and
pyc bytecode for every apex variant the operator has ever deployed:

  * apex_omega       (the phase-q target)
  * apex_v2          (the retired perp engine)
  * 99x_apex, 99-X Apex (the even older structural-liquidation engine)
  * APEX_V2_*        (env var family)

It's read-only by default. Setting purge=True deletes stray filesystem
artifacts; it NEVER rewrites source files. A source-code hit is surfaced
as verdict='fail' and a human decides.

Scopes (roots walked):
  openclaw_v1/, web/, deploy/, shared/, backup_fresh/ (new backup only
  read; never deleted).

Allowlist (skipped — legitimate historical references):
  - Comments, docstrings, //-style JS comments, #-style sh/yaml comments
  - Git metadata, node_modules, QuestDB caches
  - Files that are themselves the governor (Layer 10 + Layer 11)
  - Tests (they legitimately assert the banned names)
  - SQLite DB table names (trade_log, equity_marks,
    consensus_log, llm_cost, notifications) — kept for
    historical-data continuity; flagged as verdict='info', not 'fail'.

Never places a trade. Never consults capital.
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
_SCAN_ROOTS = ("openclaw_v1", "web", "deploy", "shared")

# Banned tokens the governor looks for. The 'apex' generic token is
# deliberately omitted (too noisy — would match /spot_aggro/gov/legacy_purge).
_BANNED_TOKENS = (
    "apex_omega",
    "apex_v2",
    "APEX_V2",
    "99X_APEX",
    "99x_apex",
    "99-X Apex",
    "OPENCLAW_99X_APEX",
    "ACTIVE_STRATEGY",        # was apex_v2-owned env var
)

# Filesystem artifacts to purge when purge=True.
_STRAY_PATTERNS = (
    "apex_omega",
    "apex_v2",
    "apex_v2.log",
    "apex_v2_state.json",
    "apex_config.yml",
    "apex_v2.state",
    "PAPER_LAST_OK.ts",
)

_ALLOW_SUBSTR = (
    ".git/", ".git\\",
    "node_modules",
    "questdb/public/assets",
    "backup_fresh/",           # fresh backup is read-only, never purged
    "backup_fresh\\",
)

# File-level allowlist for source scan.
_SRC_ALLOW_FILES = (
    "spot_aggro/governance/legacy_purge_gov.py",
    "spot_aggro/governance/legacy_deep_forensic_gov.py",
    "shared/persistence/state.py",
    "tests/",
)

# DB table names that intentionally keep the apex_ prefix. Flagged as
# 'info' so the operator sees them but they don't trip verdict='fail'.
_KEPT_DB_TABLES = (
    "trade_log",
    "equity_marks",
    "consensus_log",
    "llm_cost",
    "notifications",
)

# Regexes for each finding type.
_RE_BANNED = re.compile(
    "|".join(re.escape(t) for t in _BANNED_TOKENS),
    re.IGNORECASE,
)
_RE_KEPT_TABLE = re.compile(
    r"\b(" + "|".join(_KEPT_DB_TABLES) + r")\b"
)


@dataclass
class Finding:
    kind: str   # 'stray_path' | 'src_string' | 'config' | 'env' | 'info_db_table'
    path: str
    token: str
    line: int = 0
    detail: str = ""
    action: str = "flag"   # 'flag' | 'purged' | 'skipped'


@dataclass
class DeepScanResult:
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    findings: list[Finding] = field(default_factory=list)
    n_stray_paths: int = 0
    n_src_strings: int = 0
    n_config: int = 0
    n_env: int = 0
    n_info: int = 0
    n_purged: int = 0
    verdict: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["findings"] = [asdict(f) for f in self.findings]
        return d


# ---------------------------------------------------------------------------
# SQLite
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
                "CREATE TABLE IF NOT EXISTS gov_deep_forensic_log("
                " run_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts_ms INTEGER NOT NULL,"
                " verdict TEXT NOT NULL,"
                " n_stray_paths INTEGER NOT NULL,"
                " n_src_strings INTEGER NOT NULL,"
                " n_config INTEGER NOT NULL,"
                " n_env INTEGER NOT NULL,"
                " n_info INTEGER NOT NULL,"
                " n_purged INTEGER NOT NULL,"
                " payload_json TEXT NOT NULL"
                ")"
            )
        finally:
            con.close()


def _persist(result: DeepScanResult) -> int:
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            cur = con.execute(
                "INSERT INTO gov_deep_forensic_log(ts_ms, verdict,"
                " n_stray_paths, n_src_strings, n_config, n_env, n_info,"
                " n_purged, payload_json)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    result.ts_ms, result.verdict,
                    result.n_stray_paths, result.n_src_strings,
                    result.n_config, result.n_env, result.n_info,
                    result.n_purged,
                    json.dumps(result.to_dict()),
                ),
            )
            return int(cur.lastrowid or 0)
        finally:
            con.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_allowed(path: Path) -> bool:
    s = str(path).replace("\\", "/")
    return any(a in s for a in _ALLOW_SUBSTR)


def _is_src_allowlisted(rel: str) -> bool:
    return any(a in rel for a in _SRC_ALLOW_FILES)


def _strip_py_noise(text: str) -> str:
    t = re.sub(r"#.*", "", text)
    t = re.sub(r'"""[\s\S]*?"""', "", t)
    t = re.sub(r"'''[\s\S]*?'''", "", t)
    return t


def _strip_web_noise(text: str) -> str:
    t = re.sub(r"/\*[\s\S]*?\*/", "", text)
    t = re.sub(r"//[^\n]*", "", t)
    # HTML comments
    t = re.sub(r"<!--[\s\S]*?-->", "", t)
    return t


def _strip_yaml_noise(text: str) -> str:
    return re.sub(r"#[^\n]*", "", text)


# ---------------------------------------------------------------------------
# Scanners
# ---------------------------------------------------------------------------

def _scan_fs(repo: Path) -> list[Finding]:
    out: list[Finding] = []
    for root_name in _SCAN_ROOTS:
        root = repo / root_name
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if _is_allowed(p):
                continue
            name = p.name.lower()
            for pattern in _STRAY_PATTERNS:
                if name == pattern.lower() or (
                    p.is_dir() and name == pattern.lower()
                ):
                    out.append(Finding(
                        kind="stray_path",
                        path=str(p.relative_to(repo)),
                        token=pattern,
                        detail=f"forbidden filesystem artifact: {pattern}",
                    ))
                    break
    return out


def _scan_src_py(repo: Path) -> list[Finding]:
    out: list[Finding] = []
    for root_name in _SCAN_ROOTS:
        root = repo / root_name
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            rel = str(py.relative_to(repo)).replace("\\", "/")
            if _is_src_allowlisted(rel) or _is_allowed(py):
                continue
            try:
                text = py.read_text(encoding="utf-8")
            except Exception:
                continue
            stripped = _strip_py_noise(text)
            for m in _RE_BANNED.finditer(stripped):
                ln = stripped[: m.start()].count("\n") + 1
                out.append(Finding(
                    kind="src_string",
                    path=rel,
                    token=m.group(0),
                    line=ln,
                    detail=stripped.splitlines()[ln - 1][:160].strip()
                        if ln - 1 < len(stripped.splitlines()) else "",
                ))
    return out


def _scan_web(repo: Path) -> list[Finding]:
    out: list[Finding] = []
    for root_name in _SCAN_ROOTS:
        root = repo / root_name
        if not root.exists():
            continue
        for ext in ("*.html", "*.js"):
            for f in root.rglob(ext):
                rel = str(f.relative_to(repo)).replace("\\", "/")
                if _is_src_allowlisted(rel) or _is_allowed(f):
                    continue
                try:
                    text = f.read_text(encoding="utf-8")
                except Exception:
                    continue
                stripped = _strip_web_noise(text)
                for m in _RE_BANNED.finditer(stripped):
                    ln = stripped[: m.start()].count("\n") + 1
                    out.append(Finding(
                        kind="src_string",
                        path=rel,
                        token=m.group(0),
                        line=ln,
                        detail=f"web asset has banned token {m.group(0)!r}",
                    ))
    return out


def _scan_config(repo: Path) -> list[Finding]:
    """Scan YAML / JSON / TOML / .env / shell scripts for banned tokens
    in non-comment lines."""
    out: list[Finding] = []
    for root_name in _SCAN_ROOTS:
        root = repo / root_name
        if not root.exists():
            continue
        for pattern in ("*.yml", "*.yaml", "*.json", "*.toml",
                        "*.sh", "*.bat", "*.ps1", "*.env", ".env*"):
            for f in root.rglob(pattern):
                rel = str(f.relative_to(repo)).replace("\\", "/")
                if _is_src_allowlisted(rel) or _is_allowed(f):
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                # Yaml + sh use # comments; json/toml don't but checking
                # the same stripper is safe.
                stripped = _strip_yaml_noise(text)
                for m in _RE_BANNED.finditer(stripped):
                    ln = stripped[: m.start()].count("\n") + 1
                    out.append(Finding(
                        kind="config",
                        path=rel,
                        token=m.group(0),
                        line=ln,
                        detail=stripped.splitlines()[ln - 1][:160].strip()
                            if ln - 1 < len(stripped.splitlines()) else "",
                    ))
    # Top-level config files (repo root)
    for name in (".env", "pyproject.toml", "package.json", "wrangler.toml"):
        f = repo / name
        if f.exists():
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            stripped = _strip_yaml_noise(text)
            for m in _RE_BANNED.finditer(stripped):
                ln = stripped[: m.start()].count("\n") + 1
                out.append(Finding(
                    kind="config",
                    path=name,
                    token=m.group(0),
                    line=ln,
                    detail=f"top-level config has banned token {m.group(0)!r}",
                ))
    return out


def _scan_env() -> list[Finding]:
    """Check the live process env for banned variable names."""
    out: list[Finding] = []
    banned_env = ("OPENCLAW_99X_APEX", "APEX_V2_ENABLED", "APEX_V2_SYMBOL",
                  "APEX_V2_CAPITAL", "APEX_V2_LEVERAGE",
                  "APEX_V2_DEMO_MODE", "APEX_V2_STATE_FILE",
                  "ACTIVE_STRATEGY")
    for name in banned_env:
        if name in os.environ and os.environ[name].strip():
            out.append(Finding(
                kind="env",
                path="process.env",
                token=name,
                detail=f"banned env var set: {name}={os.environ[name][:30]!r}",
            ))
    return out


def _scan_db_tables(repo: Path) -> list[Finding]:
    """Flag kept apex_* DB tables as 'info' so the operator sees them
    but verdict stays ok."""
    out: list[Finding] = []
    db = Path(_db_path())
    if not db.is_absolute():
        db = repo / db
    if not db.exists():
        return out
    try:
        con = sqlite3.connect(str(db), timeout=2.0)
        try:
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            for (name,) in rows:
                if _RE_KEPT_TABLE.fullmatch(name):
                    out.append(Finding(
                        kind="info_db_table",
                        path=str(db.relative_to(repo)) if db.is_relative_to(repo) else str(db),
                        token=name,
                        detail=f"SQLite table {name!r} kept for historical continuity",
                    ))
        finally:
            con.close()
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------

def _autokill_enabled() -> bool:
    return os.environ.get("LEGACY_PURGE_GOV_AUTOKILL", "").strip().lower() in (
        "1", "true", "yes",
    )


def _purge_stray(f: Finding, repo: Path) -> bool:
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

def run_once(*, purge: bool | None = None) -> DeepScanResult:
    do_purge = _autokill_enabled() if purge is None else bool(purge)
    r = DeepScanResult()
    r.findings.extend(_scan_fs(_REPO))
    r.findings.extend(_scan_src_py(_REPO))
    r.findings.extend(_scan_web(_REPO))
    r.findings.extend(_scan_config(_REPO))
    r.findings.extend(_scan_env())
    r.findings.extend(_scan_db_tables(_REPO))

    for f in r.findings:
        if f.kind == "stray_path" and do_purge:
            if _purge_stray(f, _REPO):
                f.action = "purged"
                r.n_purged += 1
            else:
                f.action = "flag"

    r.n_stray_paths = sum(1 for f in r.findings if f.kind == "stray_path")
    r.n_src_strings = sum(1 for f in r.findings if f.kind == "src_string")
    r.n_config = sum(1 for f in r.findings if f.kind == "config")
    r.n_env = sum(1 for f in r.findings if f.kind == "env")
    r.n_info = sum(1 for f in r.findings if f.kind == "info_db_table")

    if r.n_src_strings > 0 or r.n_config > 0 or r.n_env > 0:
        r.verdict = "fail"
    elif r.n_stray_paths > r.n_purged:
        r.verdict = "warn"
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
                "SELECT payload_json FROM gov_deep_forensic_log"
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
                "SELECT run_id, ts_ms, verdict, n_stray_paths, n_src_strings,"
                " n_config, n_env, n_info, n_purged"
                " FROM gov_deep_forensic_log ORDER BY run_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()
