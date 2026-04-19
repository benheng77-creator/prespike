"""
Desktop → Cloud state publisher.

Runs as a daemon thread. Every 15s pushes a snapshot of engine state
to the Cloudflare Worker ingestion endpoint. Non-blocking — failures
are logged and retried with exponential backoff.

Data pushed:
    heartbeat   — every 15s (ts, uptime, mode, equity, open_pairs)
    status      — every 15s (full engine status)
    pnl         — every 60s (3h PnL report)
    trades      — every 30s (last 20 trade log rows)
    consensus   — every 30s (last 10 consensus calls)
    positions   — every 15s (open pairs)
    research    — every 300s (latest LLM research)
    governor    — every 30s (effective gates)
    llm_health  — every 60s (per-provider health)
    universe    — every 300s (top 20 coins)
    errors      — event-driven (watchdog findings)

Env vars:
    CLOUD_WORKER_URL    — e.g. https://apex-omega-api.benheng77.workers.dev
    CLOUD_INGEST_TOKEN  — shared secret for auth
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

log = logging.getLogger("apex.publisher")


_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_consecutive_failures = 0
_MAX_BACKOFF_S = 120


def _url() -> str:
    return (os.environ.get("CLOUD_WORKER_URL") or "").strip().rstrip("/")


def _token() -> str:
    return (os.environ.get("CLOUD_INGEST_TOKEN") or "").strip()


def _push(kind: str, data: dict[str, Any]) -> bool:
    """POST one snapshot to the cloud worker. Returns True on success."""
    global _consecutive_failures
    url = _url()
    token = _token()
    if not url or not token:
        return False
    body = json.dumps({"kind": kind, "data": data}, default=str).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/ingest",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "APEX-Omega-Publisher/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if 200 <= resp.status < 300:
                _consecutive_failures = 0
                return True
    except urllib.error.HTTPError as e:
        log.warning("cloud push %s HTTP %d: %s", kind, e.code, e.read().decode()[:100])
    except Exception as exc:
        log.warning("cloud push %s failed: %s", kind, exc)
    _consecutive_failures += 1
    return False


def _collect_status() -> dict[str, Any]:
    try:
        from ..api.routes import ops_status
        return ops_status()
    except Exception:
        return {"error": "collect_failed"}


def _collect_pnl() -> dict[str, Any]:
    try:
        from ..scheduler.pnl_reporter import build_report
        return build_report()
    except Exception:
        return {"error": "collect_failed"}


def _collect_trades() -> dict[str, Any]:
    try:
        from ..api.routes import ops_trades
        return ops_trades(limit=20)
    except Exception:
        return {"error": "collect_failed"}


def _collect_consensus() -> dict[str, Any]:
    try:
        from ..api.routes import ops_consensus_live
        return ops_consensus_live(limit=10)
    except Exception:
        return {"error": "collect_failed"}


def _collect_governor() -> dict[str, Any]:
    try:
        from ..api.routes import ops_governor
        return ops_governor()
    except Exception:
        return {"error": "collect_failed"}


def _collect_llm_health() -> dict[str, Any]:
    try:
        from ..api.routes import ops_llm_health
        return ops_llm_health()
    except Exception:
        return {"error": "collect_failed"}


def _collect_research() -> dict[str, Any]:
    try:
        from ..scheduler.research_scanner import get_latest
        return get_latest()
    except Exception:
        return {"error": "collect_failed"}


def _collect_universe() -> dict[str, Any]:
    try:
        from ..api.routes import ops_universe
        return ops_universe()
    except Exception:
        return {"error": "collect_failed"}


def _collect_errors() -> dict[str, Any]:
    try:
        from ..watchdog.llm_watchdog import recent_findings
        return {"findings": recent_findings(limit=10)}
    except Exception:
        return {"error": "collect_failed"}


# Schedule: (kind, collector_fn, interval_seconds)
_SCHEDULE = [
    ("heartbeat",  lambda: {
        "ts": int(time.time()),
        "uptime_s": int(time.time() - _boot_ts),
        "mode": os.environ.get("CLAW_MODE", "live"),
    }, 15),
    ("status",     _collect_status,     15),
    ("positions",  lambda: {"pairs": _collect_status().get("open_pairs", [])}, 15),
    ("pnl",        _collect_pnl,        60),
    ("trades",     _collect_trades,     30),
    ("consensus",  _collect_consensus,  30),
    ("governor",   _collect_governor,   30),
    ("llm_health", _collect_llm_health, 60),
    ("research",   _collect_research,   300),
    ("universe",   _collect_universe,   300),
    ("errors",     _collect_errors,     120),
    ("spot_aggro", lambda: _collect_spot_aggro(), 15),
    ("notifications", lambda: _safe_collect("notifications"), 30),
    ("kill",       lambda: _safe_collect("kill"),     60),
    ("llm_cost",   lambda: _safe_collect("llm_cost"), 60),
]


def _safe_collect(kind: str) -> dict[str, Any]:
    """Collect data via the API routes directly (safe, no missing functions)."""
    try:
        if kind == "notifications":
            from ..api.routes import notifications
            return notifications(limit=10)
        elif kind == "kill":
            from ..api.routes import ops_kill
            return ops_kill()
        elif kind == "llm_cost":
            from ..api.routes import llm_cost
            return llm_cost()
    except Exception:
        pass
    return {}


def _collect_spot_aggro() -> dict[str, Any]:
    try:
        from spot_aggro import _engine_instance
        if _engine_instance is None:
            return {"running": False}
        return _engine_instance.status()
    except Exception:
        return {"running": False}

_boot_ts = time.time()
_last_push: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Command executor — polls cloud for pending commands, executes locally
# ---------------------------------------------------------------------------

def _poll_commands() -> None:
    """Check cloud Worker for pending control commands and execute them."""
    url = _url()
    token = _token()
    if not url or not token:
        return
    try:
        req = urllib.request.Request(
            f"{url}/command/pending",
            headers={"User-Agent": "APEX-Omega-Publisher/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        commands = data.get("commands", [])
        if not commands:
            return
        for cmd in commands:
            _execute_command(cmd, url, token)
    except Exception as exc:
        log.debug("command poll failed: %s", exc)


def _execute_command(cmd: dict, url: str, token: str) -> None:
    """Execute a single control command from the cloud queue."""
    verb = cmd.get("verb", "").upper()
    cmd_id = cmd.get("id", "")
    reason = cmd.get("reason", "cloud")
    result = "ok"

    log.info("CLOUD COMMAND: %s (id=%s reason=%s)", verb, cmd_id, reason)

    try:
        if verb == "PAUSE":
            from ..persistence import settings as app_settings
            s = app_settings.load()
            s["engine_paused"] = True
            app_settings.save(s)
            log.info("engine paused via cloud command")

        elif verb == "RESUME":
            from ..persistence import settings as app_settings
            s = app_settings.load()
            s["engine_paused"] = False
            app_settings.save(s)
            log.info("engine resumed via cloud command")

        elif verb == "HALT":
            from spot_aggro import _engine_instance
            if _engine_instance:
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            _engine_instance.halt(f"cloud: {reason}"), loop
                        )
                    else:
                        asyncio.run(_engine_instance.halt(f"cloud: {reason}"))
                except Exception:
                    pass
            log.warning("engine HALTED via cloud command")

        elif verb == "START":
            from spot_aggro import start_engine, _engine_instance
            if _engine_instance is None:
                start_engine()
                log.info("engine started via cloud command")
            else:
                result = "already running"

        elif verb == "FLATTEN":
            from spot_aggro import _engine_instance
            if _engine_instance:
                import asyncio
                for sym in list(_engine_instance.state.positions.keys()):
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.run_coroutine_threadsafe(
                                _engine_instance._close_position(sym, "cloud_flatten"), loop
                            )
                    except Exception:
                        pass
            log.info("positions flattened via cloud command")

        else:
            result = f"unknown verb: {verb}"

    except Exception as exc:
        result = f"error: {str(exc)[:100]}"
        log.warning("cloud command %s failed: %s", verb, exc)

    # Acknowledge back to cloud
    try:
        ack_body = json.dumps({"id": cmd_id, "result": result}).encode("utf-8")
        ack_req = urllib.request.Request(
            f"{url}/command/ack",
            data=ack_body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "APEX-Omega-Publisher/1.0",
            },
        )
        urllib.request.urlopen(ack_req, timeout=5)
    except Exception as exc:
        log.debug("command ack failed: %s", exc)


def _sync_cycle() -> int:
    """Bulk push — collects ALL state into ONE KV write to stay under
    Cloudflare's free-tier limit of 1,000 writes/day.
    1 write every 2 minutes = 720/day.
    """
    bulk: dict[str, Any] = {}
    for kind, collector, _interval in _SCHEDULE:
        try:
            bulk[kind] = collector()
        except Exception as exc:
            log.debug("collector %s failed: %s", kind, exc)
            bulk[kind] = {"error": str(exc)[:100]}
    if _push("bulk", bulk):
        return 1
    return 0


def start() -> None:
    """Launch the cloud-sync daemon thread. Safe to call if unconfigured."""
    global _thread, _boot_ts
    if not _url() or not _token():
        log.info("cloud publisher disabled (CLOUD_WORKER_URL or CLOUD_INGEST_TOKEN not set)")
        return
    if _thread and _thread.is_alive():
        return
    _boot_ts = time.time()
    _stop.clear()

    def _loop():
        import logging as _lg
        _lg.basicConfig(level=_lg.INFO)
        log.info("cloud publisher started → %s", _url())
        while not _stop.is_set():
            try:
                n = _sync_cycle()
                if n:
                    log.debug("cloud sync: %d pushes", n)
                # Poll for pending cloud commands every cycle
                _poll_commands()
            except Exception:
                log.exception("cloud sync cycle crashed (recovering)")
            for _ in range(15):        # 15s between pushes (paid plan: 10M writes/day)
                if _stop.is_set():
                    return
                time.sleep(1)
        log.info("cloud publisher stopped")

    _thread = threading.Thread(target=_loop, name="apex_cloud_publisher", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
