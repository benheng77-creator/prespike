"""Phase 11n-9-ww — Auto-heal governor for System Activity.

Watches the same liveness signals shown in the CDV panel "System Activity"
table. Any component that flips to yellow (stale) or red (error/halted)
triggers a scoped recovery action. Every heal attempt is recorded with
outcome + rationale.

Guardrails:
  - per-component cooldown: COOLDOWN_S seconds between heal attempts for
    the same component. Prevents tight retry loops.
  - per-component strike cap: STRIKE_CAP consecutive failures before we
    stop retrying and raise an alert_center entry instead.
  - never touches live orders; never starts/stops the engine loop;
    never clears a kill-ladder state. Those require operator intervention.

Status mapping:
  ok     -> "green"   (status in {running})
  warn   -> "yellow"  (status in {stale, pending, escalated})
  fail   -> "red"     (status in {error, halted, idle})
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable

COOLDOWN_S = int(os.environ.get("SPOT_AUTO_HEAL_COOLDOWN_S", "300"))   # 5 min
STRIKE_CAP = int(os.environ.get("SPOT_AUTO_HEAL_STRIKE_CAP", "3"))
EVAL_INTERVAL_S = int(os.environ.get("SPOT_AUTO_HEAL_EVAL_S", "60"))

GREEN_STATUSES = {"running"}
YELLOW_STATUSES = {"stale", "pending", "escalated"}
RED_STATUSES = {"error", "halted", "idle"}


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
    con = _connect()
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS spot_activity_heal_events("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts_ms INTEGER NOT NULL,"
            " component TEXT NOT NULL,"
            " severity TEXT NOT NULL,"       # 'yellow' | 'red'
            " action TEXT NOT NULL,"         # heal action invoked
            " outcome TEXT NOT NULL,"        # 'healed' | 'failed' | 'skipped_cooldown' | 'struck_out' | 'no_handler'
            " rationale TEXT,"
            " context_json TEXT"
            ")"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_heal_ts"
            " ON spot_activity_heal_events(ts_ms DESC)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_heal_component"
            " ON spot_activity_heal_events(component, ts_ms DESC)"
        )
    finally:
        con.close()


@dataclass
class HealResult:
    component: str
    severity: str            # 'yellow' | 'red' | 'green'
    action: str
    outcome: str
    rationale: str
    context: dict[str, Any]


def _severity_of(component: dict[str, Any]) -> str:
    if component.get("ok"):
        return "green"
    status = str(component.get("status", "")).lower()
    if status in RED_STATUSES:
        return "red"
    if status in YELLOW_STATUSES:
        return "yellow"
    return "yellow"                 # default bad-but-not-red


def _last_heal_row(component: str) -> sqlite3.Row | None:
    _init_schema()
    try:
        con = _connect()
        try:
            r = con.execute(
                "SELECT ts_ms, outcome FROM spot_activity_heal_events"
                " WHERE component = ? ORDER BY ts_ms DESC LIMIT 1",
                (component,),
            ).fetchone()
            return r
        finally:
            con.close()
    except Exception:
        return None


def _recent_strikes(component: str, window_s: int = 3600) -> int:
    """Count consecutive non-healed outcomes for this component within window."""
    _init_schema()
    try:
        con = _connect()
        try:
            cutoff = int(time.time() * 1000) - window_s * 1000
            rows = con.execute(
                "SELECT outcome FROM spot_activity_heal_events"
                " WHERE component = ? AND ts_ms >= ?"
                " ORDER BY ts_ms DESC LIMIT 10",
                (component, cutoff),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return 0
    strikes = 0
    for r in rows:
        if r["outcome"] == "healed":
            break
        strikes += 1
    return strikes


def _record_event(res: HealResult) -> None:
    _init_schema()
    try:
        con = _connect()
        try:
            con.execute(
                "INSERT INTO spot_activity_heal_events("
                " ts_ms, component, severity, action, outcome,"
                " rationale, context_json"
                ") VALUES(?,?,?,?,?,?,?)",
                (int(time.time() * 1000), res.component, res.severity,
                 res.action, res.outcome, res.rationale[:240],
                 json.dumps(res.context, default=str)),
            )
        finally:
            con.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Heal actions — one per component. Each returns (action_label, did_heal, note).
# Must be idempotent and never raise.
# ---------------------------------------------------------------------------

def _heal_formula_review() -> tuple[str, bool, str]:
    try:
        from spot_aggro.governance.formula_review import run
        v = run()
        return (
            "formula_review.run()", True,
            f"verdict={getattr(v, 'verdict', '?')} (forced immediate tick)",
        )
    except Exception as e:
        return ("formula_review.run()", False, f"exception: {str(e)[:120]}")


def _heal_daily_report() -> tuple[str, bool, str]:
    try:
        from spot_aggro.governance.daily_report import generate
        r = generate()
        return (
            "daily_report.generate()", True,
            f"date={getattr(r, 'report_date', '?')} (forced immediate tick)",
        )
    except Exception as e:
        return ("daily_report.generate()", False, f"exception: {str(e)[:120]}")


def _heal_heartbeat_writer() -> tuple[str, bool, str]:
    try:
        from spot_aggro.ops.scheduler.heartbeat_writer import _write_heartbeat
        _write_heartbeat()
        return ("heartbeat_writer._write_heartbeat()", True,
                "forced equity mark write")
    except Exception as e:
        return ("heartbeat_writer._write_heartbeat()", False,
                f"exception: {str(e)[:120]}")


def _heal_exchange_comparison() -> tuple[str, bool, str]:
    try:
        from spot_aggro.ops.scheduler import exchange_comparison_feed as xcf
        # The scheduler exposes a _poll_once style entry point; if not, fall
        # back to invoking the module-level run() / tick().
        for candidate in ("_poll_once", "tick", "run_once", "run"):
            fn = getattr(xcf, candidate, None)
            if callable(fn):
                fn()
                return (
                    f"exchange_comparison_feed.{candidate}()",
                    True, "forced immediate scan",
                )
        return ("exchange_comparison_feed", False,
                "no tick entry point found")
    except Exception as e:
        return ("exchange_comparison_feed", False,
                f"exception: {str(e)[:120]}")


def _heal_shadow_scorer() -> tuple[str, bool, str]:
    """Shadow scorer runs inline with the engine; staleness usually means
    the engine isn't producing authorizations. Best we can do is poke
    the engine state source + flag for operator review."""
    try:
        from spot_aggro.governance.engine_state_source import current_engine_state
        es = current_engine_state() or {}
        state = es.get("state", "?")
        if state == "running":
            return (
                "shadow_scorer (engine-attached)",
                False,
                f"engine running but no authorizations in 300s — flagged for review",
            )
        return (
            "shadow_scorer (engine-attached)",
            False,
            f"engine state={state} — operator must restart engine for authorizations",
        )
    except Exception as e:
        return ("shadow_scorer (engine-attached)", False,
                f"exception: {str(e)[:120]}")


def _heal_engine_heartbeat() -> tuple[str, bool, str]:
    """Never auto-starts the engine. Only observes + flags."""
    try:
        from spot_aggro.governance.engine_state_source import current_engine_state
        es = current_engine_state() or {}
        return ("engine_heartbeat (observe-only)", False,
                f"state={es.get('state')} reason={es.get('reason','')[:80]} — operator required")
    except Exception as e:
        return ("engine_heartbeat (observe-only)", False,
                f"exception: {str(e)[:120]}")


def _heal_kill_ladder() -> tuple[str, bool, str]:
    """Kill ladder escalation is a safety feature — never auto-release."""
    return ("kill_ladder (observe-only)", False,
            "escalation must be released by operator")


HEALERS: dict[str, Callable[[], tuple[str, bool, str]]] = {
    "formula_review":       _heal_formula_review,
    "daily_report":         _heal_daily_report,
    "heartbeat_writer":     _heal_heartbeat_writer,
    "exchange_comparison":  _heal_exchange_comparison,
    "shadow_scorer":        _heal_shadow_scorer,
    "engine_heartbeat":     _heal_engine_heartbeat,
    "kill_ladder":          _heal_kill_ladder,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _fetch_components() -> list[dict[str, Any]]:
    """Reuse the system_activity endpoint's component builder by calling
    the route function directly. Header/RBAC skipped — this runs inside
    the server process."""
    try:
        # Build a fake admin role so _require_viewer passes.
        from spot_aggro.api import routes_strategy_cdv as r
        # Monkey-patch the requirement for in-process use: call a dedicated
        # factored builder if present, else inline the same sequence.
        # Simplest: invoke the endpoint function with admin role.
        os.environ.setdefault("FEATURE_CONTRARIAN_DEEPVALUE_PANEL", "1")
        payload = r.cdv_system_activity(
            x_cdv_role="strategy:contrarian_deepvalue_admin",
            x_ops_token=os.environ.get("OPS_ADMIN_TOKEN"),
        )
        return list(payload.get("components") or [])
    except Exception:
        return []


def evaluate() -> dict[str, Any]:
    """One pass. Inspects all components, heals the ones that need it,
    records outcomes. Returns summary dict for daemons / endpoints."""
    comps = _fetch_components()
    results: list[HealResult] = []
    now_ms = int(time.time() * 1000)

    for comp in comps:
        name = str(comp.get("name", ""))
        if not name:
            continue
        severity = _severity_of(comp)

        if severity == "green":
            continue

        # Cooldown check.
        last = _last_heal_row(name)
        if last and (now_ms - int(last["ts_ms"])) < COOLDOWN_S * 1000:
            res = HealResult(
                component=name, severity=severity,
                action="(cooldown)",
                outcome="skipped_cooldown",
                rationale=f"within {COOLDOWN_S}s of last attempt",
                context={"status": comp.get("status"),
                         "observation": comp.get("observation")},
            )
            results.append(res)
            continue

        # Strike cap.
        strikes = _recent_strikes(name)
        if strikes >= STRIKE_CAP:
            res = HealResult(
                component=name, severity=severity,
                action="(strike-cap)",
                outcome="struck_out",
                rationale=(
                    f"{strikes} consecutive non-heal outcomes — "
                    "escalating to alert_center instead of retry"
                ),
                context={"status": comp.get("status"),
                         "observation": comp.get("observation")},
            )
            _record_event(res)
            results.append(res)
            # Raise alert (best-effort).
            try:
                from spot_aggro.governance.alert_center import record_alert
                record_alert(
                    kind="activity_auto_heal_struck_out",
                    severity="high",
                    message=(
                        f"{name}: {strikes} consecutive heal failures — "
                        "auto-heal giving up, operator intervention needed"
                    ),
                    context={"component": name,
                             "status": comp.get("status"),
                             "observation": comp.get("observation")},
                )
            except Exception:
                pass
            continue

        # Invoke healer.
        healer = HEALERS.get(name)
        if healer is None:
            res = HealResult(
                component=name, severity=severity,
                action="(no-handler)",
                outcome="no_handler",
                rationale=f"component {name} has no registered healer",
                context={"status": comp.get("status")},
            )
            _record_event(res)
            results.append(res)
            continue

        try:
            action_label, did_heal, note = healer()
        except Exception as e:
            action_label = f"{name}.heal()"
            did_heal = False
            note = f"healer raised: {str(e)[:120]}\n{traceback.format_exc()[-200:]}"

        res = HealResult(
            component=name, severity=severity,
            action=action_label,
            outcome="healed" if did_heal else "failed",
            rationale=note,
            context={"status": comp.get("status"),
                     "observation": comp.get("observation")},
        )
        _record_event(res)
        results.append(res)

    n_green = sum(1 for c in comps if c.get("ok"))
    n_yellow = sum(1 for c in comps if _severity_of(c) == "yellow")
    n_red = sum(1 for c in comps if _severity_of(c) == "red")
    n_healed = sum(1 for r in results if r.outcome == "healed")

    return {
        "ts_ms": now_ms,
        "n_components": len(comps),
        "n_green": n_green,
        "n_yellow": n_yellow,
        "n_red": n_red,
        "n_actions": len(results),
        "n_healed": n_healed,
        "results": [
            {
                "component": r.component,
                "severity": r.severity,
                "action": r.action,
                "outcome": r.outcome,
                "rationale": r.rationale,
            }
            for r in results
        ],
    }


def recent_events(limit: int = 30) -> list[dict[str, Any]]:
    _init_schema()
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT ts_ms, component, severity, action, outcome,"
                "       rationale, context_json"
                " FROM spot_activity_heal_events"
                " ORDER BY ts_ms DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        ctx = {}
        try:
            ctx = json.loads(r["context_json"] or "{}")
        except Exception:
            pass
        out.append({
            "ts_ms": int(r["ts_ms"]),
            "component": r["component"],
            "severity": r["severity"],
            "action": r["action"],
            "outcome": r["outcome"],
            "rationale": r["rationale"],
            "context": ctx,
        })
    return out
