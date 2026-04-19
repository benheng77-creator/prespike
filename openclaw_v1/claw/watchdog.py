"""
Claw watchdog — infra probes + self-heal for infrastructure ONLY.

What the watchdog may do:
  * probe DB, API, WS, exchange session, clock skew, disk
  * write results to claw_health_probes
  * when a probe fails, record a claw_incident, attempt a bounded
    infrastructure-only remediation (reopen DB, restart WS client),
    and emit the auto_action taken on the incident row.

What the watchdog must NOT do:
  * touch the bot brain (strategies/, binary15m/, binary15/, apex_v2/)
  * modify bot_decisions_immutable or any decision row
  * alter strategy parameters, thresholds, or gates
  * inject orders on the bot's behalf

Every probe returns a Probe dataclass and writes one row. The probe registry
is explicit — callers inject probes so the watchdog stays exchange-agnostic.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional

from .db import claw_db_path, connect, init_claw_schema
from .incidents import open_incident


log = logging.getLogger("claw.watchdog")

ProbeFn = Callable[[], "ProbeResult"]


@dataclass
class ProbeResult:
    probe: str
    ok: bool
    latency_ms: float
    detail: str = ""
    auto_action: Optional[str] = None


@dataclass
class WatchdogReport:
    probes: list[ProbeResult] = field(default_factory=list)
    incidents_opened: int = 0


# ---------------------------------------------------------------------------
# Built-in probes
# ---------------------------------------------------------------------------

def probe_db(db_path: Optional[str] = None) -> ProbeResult:
    """Open a SQLite connection, run 'SELECT 1', measure latency."""
    path = claw_db_path(db_path)
    started = time.time()
    try:
        con = sqlite3.connect(path, timeout=1.0)
        con.execute("SELECT 1").fetchone()
        con.close()
        return ProbeResult(
            probe="db", ok=True,
            latency_ms=(time.time() - started) * 1000.0,
            detail=path,
        )
    except Exception as exc:
        return ProbeResult(
            probe="db", ok=False,
            latency_ms=(time.time() - started) * 1000.0,
            detail=f"{type(exc).__name__}: {exc}",
        )


def probe_disk(threshold_mb: float = 200.0) -> ProbeResult:
    """Check free disk space on the DB drive."""
    path = claw_db_path()
    started = time.time()
    try:
        if os.name == "nt":
            import ctypes
            free_bytes = ctypes.c_ulonglong(0)
            ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(os.path.dirname(os.path.abspath(path)) or "."),
                None, None, ctypes.byref(free_bytes),
            )
            free_mb = free_bytes.value / (1024 * 1024)
        else:
            st = os.statvfs(os.path.dirname(os.path.abspath(path)) or ".")
            free_mb = (st.f_bavail * st.f_frsize) / (1024 * 1024)
        ok = free_mb >= threshold_mb
        return ProbeResult(
            probe="disk", ok=ok,
            latency_ms=(time.time() - started) * 1000.0,
            detail=f"free_mb={free_mb:.1f} threshold_mb={threshold_mb:.1f}",
        )
    except Exception as exc:
        return ProbeResult(
            probe="disk", ok=False,
            latency_ms=(time.time() - started) * 1000.0,
            detail=f"{type(exc).__name__}: {exc}",
        )


def probe_clock(ntp_skew_ms: Optional[float] = None,
                threshold_ms: float = 30_000.0) -> ProbeResult:
    """System clock sanity. ``ntp_skew_ms`` may be supplied by the caller
    if an NTP client is available; otherwise we only check that time moves
    forward in a monotonically sensible way.
    """
    started = time.time()
    t0 = time.time()
    time.sleep(0.001)
    t1 = time.time()
    ok = t1 >= t0
    detail = "forward_ok"
    if ntp_skew_ms is not None:
        ok = ok and abs(ntp_skew_ms) < threshold_ms
        detail = f"skew_ms={ntp_skew_ms:.1f} threshold={threshold_ms:.0f}"
    return ProbeResult(
        probe="clock", ok=ok,
        latency_ms=(time.time() - started) * 1000.0,
        detail=detail,
    )


def make_fastapi_probe(url: str, timeout_s: float = 1.5) -> ProbeFn:
    """Returns a probe that fetches a URL (use with /health)."""
    def _probe() -> ProbeResult:
        started = time.time()
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                ok = 200 <= resp.status < 300
            return ProbeResult(
                probe="fastapi", ok=ok,
                latency_ms=(time.time() - started) * 1000.0,
                detail=url,
            )
        except Exception as exc:
            return ProbeResult(
                probe="fastapi", ok=False,
                latency_ms=(time.time() - started) * 1000.0,
                detail=f"{type(exc).__name__}: {exc}",
            )
    return _probe


def make_ws_heartbeat_probe(
    get_age_seconds: Callable[[], Optional[float]],
    *,
    max_age_s: float = 10.0,
) -> ProbeFn:
    """Wraps a caller-supplied callable that returns WS heartbeat age (seconds).

    Returning None = no heartbeat ever received → fail.
    """
    def _probe() -> ProbeResult:
        started = time.time()
        age = get_age_seconds()
        if age is None:
            return ProbeResult(probe="ws", ok=False,
                               latency_ms=(time.time() - started) * 1000.0,
                               detail="no_heartbeat_yet")
        ok = age < max_age_s
        return ProbeResult(probe="ws", ok=ok,
                           latency_ms=(time.time() - started) * 1000.0,
                           detail=f"age_s={age:.1f} max_s={max_age_s:.1f}")
    return _probe


def make_exchange_session_probe(
    session_age_seconds: Callable[[], Optional[float]],
    *,
    max_age_s: float = 30.0,
) -> ProbeFn:
    """Caller-supplied: age of last successful REST/ticker response."""
    def _probe() -> ProbeResult:
        started = time.time()
        age = session_age_seconds()
        if age is None:
            return ProbeResult(probe="exchange", ok=False,
                               latency_ms=(time.time() - started) * 1000.0,
                               detail="no_session")
        ok = age < max_age_s
        return ProbeResult(probe="exchange", ok=ok,
                           latency_ms=(time.time() - started) * 1000.0,
                           detail=f"age_s={age:.1f} max_s={max_age_s:.1f}")
    return _probe


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_probes(
    probes: Iterable[ProbeFn],
    *,
    db_path: Optional[str] = None,
    auto_heal: Optional[Mapping[str, Callable[[ProbeResult], Optional[str]]]] = None,
) -> WatchdogReport:
    """Execute every probe once, record results, open incidents on failure.

    ``auto_heal`` is a mapping from probe name to a callable that attempts
    an infrastructure-only remediation and returns a short description of
    what was done. It is ONLY called when the probe fails. The callable is
    expected to affect only infra (reopen a connection, swap WS→REST, etc.).

    The watchdog itself never touches strategy code; ``auto_heal`` callables
    are the caller's responsibility and should be unit-tested against the
    non-interference contract.
    """
    report = WatchdogReport()
    init_claw_schema(db_path)
    for probe in probes:
        try:
            result = probe()
        except Exception as exc:                    # pragma: no cover - defensive
            log.exception("probe raised")
            result = ProbeResult(
                probe=getattr(probe, "__name__", "probe"),
                ok=False, latency_ms=0.0,
                detail=f"{type(exc).__name__}: {exc}",
            )

        if not result.ok and auto_heal and result.probe in auto_heal:
            try:
                action = auto_heal[result.probe](result)
                if action:
                    result.auto_action = action
            except Exception as exc:
                log.exception("auto_heal for %s raised", result.probe)
                result.detail += f" | auto_heal_failed:{type(exc).__name__}"

        # Write the probe row on its own connection, then release before
        # opening an incident (incidents use their own connection).
        con = connect(db_path)
        try:
            _write_probe(con, result)
            con.commit()
        finally:
            con.close()

        if not result.ok:
            open_incident(
                kind=_probe_kind(result.probe),
                severity="warn",
                component=result.probe,
                message=result.detail or f"{result.probe} failed",
                auto_action=result.auto_action,
                metadata={"probe": result.probe,
                          "latency_ms": result.latency_ms},
                db_path=db_path,
            )
            report.incidents_opened += 1
            # Best-effort notify fan-out — never allowed to crash the watchdog.
            try:
                from . import notify as _notify
                _notify.notify(
                    f"{result.probe} probe failed: {result.detail or 'no detail'}",
                    severity="warn", tag="claw.watchdog",
                )
            except Exception:
                log.exception("claw.notify failed (non-blocking)")

        report.probes.append(result)
    return report


def list_recent_probes(
    *,
    limit: int = 50,
    probe: Optional[str] = None,
    db_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    con = connect(db_path)
    try:
        if probe:
            rows = con.execute(
                "SELECT * FROM claw_health_probes WHERE probe = ? "
                "ORDER BY id DESC LIMIT ?", (probe, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM claw_health_probes ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def _write_probe(con: sqlite3.Connection, r: ProbeResult) -> None:
    con.execute(
        "INSERT INTO claw_health_probes (ts_ms, probe, ok, latency_ms, detail) "
        "VALUES (?, ?, ?, ?, ?)",
        (int(time.time() * 1000), r.probe, 1 if r.ok else 0,
         float(r.latency_ms), r.detail),
    )


def _probe_kind(probe: str) -> str:
    if probe == "db": return "db"
    if probe == "fastapi": return "rest"
    if probe == "ws": return "ws"
    if probe == "exchange": return "exchange"
    if probe == "clock": return "clock"
    if probe == "disk": return "disk"
    return "other"
