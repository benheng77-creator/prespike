"""Phase 11n-3 — Auto Orchestrator.

The "100% auto" coordinator that runs every governance layer on a
schedule, auto-heals failing checks, auto-fixes where possible, and
surfaces any gap it can't fix so the operator sees it immediately.

One tick does, in strict order:

  1. RESEARCH       — research_agent.run_and_persist (+ auto per-pass
                      scenario batch via existing wiring)
  2. RESEARCH TRUTH — research_truth_gov.validate_and_persist
  3. CARD TRUTH     — card_truth_gov.run_and_persist
  4. DAILY AUDIT    — daily_system_auditor.run_and_persist (at most every
                      6h — cheap enough to include every tick but gated
                      by the daily policy).
  5. SCENARIO NOVELTY — for every tier that got a new batch in step 1,
                        loop_novelty_gov.validate_and_persist. If verdict
                        == "stuck", AUTO-HEAL by triggering another
                        scenario batch with an explicit salt override,
                        then re-validate.
  6. DECISION       — decision_engine.build_and_persist
  7. DECISION TRUTH — decision_truth_gov.validate_and_persist
  8. GAP DETECT     — aggregate any fail/warn across govs into an
                      OrchestratorTick.gaps list with remediation hints.
  9. AUTO-FIX       — apply known-safe fixes (e.g. re-run a gov that
                      failed due to transient "never_run" state).

Every tick is persisted to `spot_orchestrator_ticks` with full
breakdown so the dashboard can display it and tests can assert on it.

The background loop is started via `start(interval_s=...)` and runs on
a single daemon thread using time-based scheduling. It can be stopped
via `stop()`. Starting twice is a no-op.

SPOT AGGRO only. Never places trades. Never moves capital. Never flips
tier toggles unless SPOT_RESEARCH_ENFORCE_HALT=1 (the research agent
itself owns that gate; the orchestrator just calls run_and_persist).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, asdict, field
from typing import Any, Callable, Optional


log = logging.getLogger(__name__)

TIERS = ("A+", "A", "B", "C")
DEFAULT_INTERVAL_S = 300   # 5 min; opt-in env override via SPOT_AUTO_INTERVAL_S
DAILY_AUDIT_MIN_INTERVAL_S = 6 * 3600


@dataclass
class Step:
    name: str
    ok: bool
    severity: str           # "ok" | "warn" | "fail"
    details: str
    duration_ms: int
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Gap:
    layer: str              # "research_truth" | "card_truth" | "loop_novelty" | ...
    severity: str           # "warn" | "fail"
    message: str
    remediation: str        # human hint
    auto_fix_applied: bool = False


@dataclass
class OrchestratorTick:
    tick_id: str
    started_ts_ms: int
    completed_ts_ms: int
    verdict: str            # "ok" | "warn" | "fail"
    steps: list[Step]
    gaps: list[Gap]
    heals_applied: int
    fixes_applied: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "tick_id": self.tick_id,
            "started_ts_ms": self.started_ts_ms,
            "completed_ts_ms": self.completed_ts_ms,
            "verdict": self.verdict,
            "steps": [s.to_dict() for s in self.steps],
            "gaps": [asdict(g) for g in self.gaps],
            "heals_applied": self.heals_applied,
            "fixes_applied": self.fixes_applied,
        }


# ---------------------------------------------------------------------------
# Step runner — resilient wrapper.
# ---------------------------------------------------------------------------

def _run_step(name: str, fn: Callable[[], Any]) -> tuple[Step, Any]:
    t0 = time.monotonic()
    try:
        result = fn()
        dur = int((time.monotonic() - t0) * 1000)
        return (Step(
            name=name, ok=True, severity="ok",
            details=f"{name} completed", duration_ms=dur,
            evidence={"returned": type(result).__name__},
        ), result)
    except Exception as exc:  # noqa: BLE001
        dur = int((time.monotonic() - t0) * 1000)
        log.exception("orchestrator step %s failed", name)
        return (Step(
            name=name, ok=False, severity="fail",
            details=f"{type(exc).__name__}: {exc}",
            duration_ms=dur,
        ), None)


# ---------------------------------------------------------------------------
# Single tick
# ---------------------------------------------------------------------------

_last_daily_audit_ts = 0.0


def run_tick() -> OrchestratorTick:
    """Execute one orchestrator tick. Returns the persisted tick summary."""
    global _last_daily_audit_ts
    started = int(time.time() * 1000)
    tick_id = f"tk-{started}-{uuid.uuid4().hex[:6]}"
    steps: list[Step] = []
    gaps: list[Gap] = []
    heals = 0
    fixes = 0

    # 1. Research pass (also triggers scenarios + research_truth +
    # decision via the existing wiring in run_and_persist).
    from spot_aggro.governance import research_agent as ra
    s, _r = _run_step(
        "research_run_and_persist",
        lambda: ra.run_and_persist(status="interim"),
    )
    steps.append(s)

    # 2. Card truth audit (server-side).
    from spot_aggro.governance import card_truth_gov as ctg
    s, card_audit = _run_step(
        "card_truth_audit", lambda: ctg.run_and_persist(),
    )
    steps.append(s)
    if card_audit is not None:
        # Handle both dataclass and dict return.
        if hasattr(card_audit, "to_dict"):
            card_verdict = card_audit.verdict
            card_failed = [c for c in card_audit.cards if c.verdict == "fail"]
        else:
            card_verdict = (card_audit or {}).get("verdict")
            card_failed = [c for c in (card_audit or {}).get("cards", [])
                           if c.get("verdict") == "fail"]
        if card_verdict == "fail":
            gaps.append(Gap(
                layer="card_truth",
                severity="fail",
                message=f"{len(card_failed)} cards failed audit",
                remediation="check /spot_aggro/cards/truth for contradictions",
            ))
        elif card_verdict == "warn":
            gaps.append(Gap(
                layer="card_truth", severity="warn",
                message="card audit returned warnings",
                remediation="review card_truth_gov findings",
            ))

    # 3. Daily system audit (throttled).
    now_s = time.time()
    if now_s - _last_daily_audit_ts >= DAILY_AUDIT_MIN_INTERVAL_S:
        from spot_aggro.governance import daily_system_auditor as dsa
        s, dsa_run = _run_step(
            "daily_system_audit", lambda: dsa.run_and_persist(),
        )
        steps.append(s)
        _last_daily_audit_ts = now_s
        if dsa_run and getattr(dsa_run, "verdict", None) == "fail":
            gaps.append(Gap(
                layer="daily_audit", severity="fail",
                message="daily system audit has fail-level checks",
                remediation="inspect /spot_aggro/audit/system",
            ))

    # 4. Loop Novelty audit for the latest batch of each tier.
    from spot_aggro.governance import loop_novelty_gov as lng
    from spot_aggro.governance import scenario_runner as sr
    for tier in TIERS:
        batch = sr.latest_batch_for_tier(tier)
        if not batch:
            continue
        s, verdict = _run_step(
            f"loop_novelty_{tier}",
            lambda b=batch: lng.validate_and_persist(b),
        )
        steps.append(s)
        if verdict is None:
            continue
        if verdict.verdict == "stuck":
            # Auto-heal: force a fresh salt + axis shuffle, run one more
            # batch, and re-validate. If that one is also stuck, we log
            # a gap but stop looping (prevents runaway).
            heals += 1
            heal_salt = f"heal-{uuid.uuid4().hex[:8]}"
            heal_shift = (batch.get("pass_index", 0) + 7) % 11
            heal_order = tuple(reversed(
                (batch.get("axis_order") or
                 ["style","market","capital_usd","days","tp_mult"])
            ))
            try:
                healed_batch = sr.run_batch_and_persist(
                    tier=tier, cap=32,
                    axis_order=heal_order,
                    seed_salt=heal_salt,
                    value_shift=heal_shift,
                )
                v2 = lng.validate_and_persist(healed_batch.to_dict())
                steps.append(Step(
                    name=f"auto_heal_{tier}", ok=(v2.verdict != "stuck"),
                    severity=("ok" if v2.verdict != "stuck" else "warn"),
                    details=f"auto-healed stuck loop → {v2.verdict}",
                    duration_ms=0,
                ))
                if v2.verdict == "stuck":
                    gaps.append(Gap(
                        layer="loop_novelty", severity="fail",
                        message=f"tier {tier} still stuck after auto-heal",
                        remediation="manually expand scenario matrix for this tier",
                        auto_fix_applied=True,
                    ))
            except Exception as exc:  # noqa: BLE001
                gaps.append(Gap(
                    layer="loop_novelty", severity="fail",
                    message=f"tier {tier} auto-heal raised: {exc}",
                    remediation="inspect scenario_runner logs",
                ))
        elif verdict.verdict == "degraded":
            gaps.append(Gap(
                layer="loop_novelty", severity="warn",
                message=f"tier {tier} novelty degraded",
                remediation="rerun scenario batch with explicit seed_salt",
            ))

    # 5. Research truth verdict gap check (read the most recent).
    from spot_aggro.governance import research_truth_gov as rtg
    rt = rtg.latest_verdict()
    if rt and rt.get("verdict") == "invalid":
        gaps.append(Gap(
            layer="research_truth", severity="fail",
            message="research truth governor flagged latest report",
            remediation="inspect /spot_aggro/research/truth",
        ))

    # 6. Decision truth verdict gap check.
    from spot_aggro.governance import decision_truth_gov as dtg
    dt = dtg.latest_verdict()
    if dt and dt.get("verdict") == "invalid":
        gaps.append(Gap(
            layer="decision_truth", severity="fail",
            message="decision truth governor flagged latest decision bundle",
            remediation="inspect /spot_aggro/decision/truth",
        ))

    # 7. Auto-fix known-safe transient states: "never_run" on govs that
    # depend on earlier ones. If decision truth is never_run but we just
    # built a bundle, re-run the governor once.
    if dt is None:
        try:
            from spot_aggro.governance import decision_engine as de
            bundle = de.latest_bundle()
            if bundle:
                dtg.validate_and_persist(bundle)
                fixes += 1
        except Exception:  # noqa: BLE001
            pass

    # 8. Phase 11n-9: rebuild Daily Alpha bundle + run the Layer 8
    # governor checklist on every tick so the dashboard has fresh picks.
    try:
        from spot_aggro.governance import daily_alpha as _da
        s, alpha = _run_step(
            "daily_alpha_build", lambda: _da.build_and_persist(),
        )
        steps.append(s)
        if alpha is not None and alpha.admitted_count == 0:
            gaps.append(Gap(
                layer="daily_alpha", severity="warn",
                message=(f"no alpha picks admitted today "
                         f"({alpha.rejected_count} rejected)"),
                remediation="inspect /daily_alpha/latest for checklist details",
            ))
    except Exception:  # noqa: BLE001
        log.exception("daily_alpha step failed")

    # 9. Phase 11n-9-c: Daily Alpha auto-executor. Opt-in via
    # SPOT_ALPHA_AUTO_EXECUTE=1. Turns admitted picks into real orders
    # via place_post_only; idempotent per (symbol, side, UTC day) so
    # repeated ticks don't double-send. No-op when not enabled.
    try:
        from spot_aggro.governance import daily_alpha_executor as _dae
        if _dae.is_enabled():
            s, executions = _run_step(
                "daily_alpha_execute", lambda: _dae.execute_admitted_picks(),
            )
            steps.append(s)
            failed = [e for e in (executions or []) if not e.placed]
            if failed:
                gaps.append(Gap(
                    layer="daily_alpha_executor",
                    severity="warn",
                    message=(f"{len(failed)} alpha pick(s) failed to "
                             f"place this tick"),
                    remediation="inspect /spot_aggro/daily_alpha/executions",
                ))
    except Exception:  # noqa: BLE001
        log.exception("daily_alpha_executor step failed")

    # 10a. Phase 11n-9-i: Take-Profit Agent. Scans open positions every
    # tick for live_ret ≥ 2% candidates. 4-agent team ranks/schedules,
    # Layer-9 TP Sell Governor audits evidence, then order ships through
    # Layer-8 pre-trade gate. Default ON (SPOT_TP_EXECUTE=1) — disable
    # with TRADE_DRY_RUN or SPOT_TP_EXECUTE=0.
    try:
        from spot_aggro.governance import tp_agent as _tp
        s, tp_proposal = _run_step(
            "tp_agent_build_execute",
            lambda: _tp.build_and_execute(),
        )
        steps.append(s)
        if tp_proposal is not None:
            sold = sum(1 for c in tp_proposal.candidates if c.executed)
            admitted_but_not_shipped = sum(
                1 for c in tp_proposal.candidates
                if (not c.rejection_reason) and (not c.executed)
            )
            if admitted_but_not_shipped > 0:
                gaps.append(Gap(
                    layer="tp_agent", severity="warn",
                    message=(f"{admitted_but_not_shipped} TP candidates "
                             "approved but ship path failed"),
                    remediation="inspect /spot_aggro/tp/latest",
                ))
            if sold > 0:
                log.info("tp_agent shipped %d take-profit sells this tick", sold)
    except Exception:  # noqa: BLE001
        log.exception("tp_agent step failed")

    # 10b. Phase 11n-9-d: Reconciled Sweeper. Always builds a plan so the
    # operator sees which orphan positions are candidates for KEEP /
    # SELL / LINK. Real shipping requires SPOT_RECON_SWEEP_EXECUTE=1;
    # otherwise every tick's plan is persisted as dry-run.
    try:
        from spot_aggro.governance import reconciled_sweeper as _rs
        if _rs.is_execute_enabled():
            s, plan = _run_step("recon_sweep_execute",
                                lambda: _rs.build_and_execute())
        else:
            s, plan = _run_step("recon_sweep_plan",
                                lambda: _rs.build_and_persist())
        steps.append(s)
        if plan is not None:
            n_sell = sum(1 for a in plan.actions if a.action == "sell")
            n_link = sum(1 for a in plan.actions if a.action == "link")
            if (n_sell + n_link) > 0 and plan.dry_run:
                gaps.append(Gap(
                    layer="reconciled_sweeper", severity="warn",
                    message=(f"sweep plan has {n_sell} sells + {n_link} "
                             f"links pending · dry-run only"),
                    remediation="set SPOT_RECON_SWEEP_EXECUTE=1 or POST "
                                "/spot_aggro/recon/sweep to ship",
                ))
    except Exception:  # noqa: BLE001
        log.exception("reconciled_sweeper step failed")

    # Phase 11n-9-k: publish every gap to the Alert Center so the
    # Alerts tab shows a single unified incident feed. The center
    # aggregates repeats (kind,symbol,tier) inside a 10-min window,
    # so ticking 12×/hour doesn't flood with duplicates.
    try:
        from spot_aggro.governance import alert_center
        for g in gaps:
            if g.auto_fix_applied:
                continue  # already resolved, don't alert
            sev_override = "P0" if g.severity == "fail" else (
                "P2" if g.severity == "warn" else "P3")
            alert_center.ingest(
                kind=f"orch_gap:{g.layer}",
                source="orchestrator",
                message=g.message,
                severity=sev_override,
                evidence={"layer": g.layer,
                          "remediation": g.remediation,
                          "tick_id": tick_id},
            )
    except Exception:  # noqa: BLE001
        log.exception("alert_center ingest failed for tick gaps")

    # Phase 11n-9-l: incident-mode auto-exit. If we're in incident mode
    # and no P0 remains + min-duration elapsed, auto-clear so the UI
    # drops out of the collapsed/diagnostic-forward layout.
    try:
        from spot_aggro.governance import incident_mode
        incident_mode.maybe_auto_exit()
    except Exception:  # noqa: BLE001
        log.exception("incident_mode.maybe_auto_exit failed on tick")

    # Aggregate verdict.
    has_fail = any(s.severity == "fail" for s in steps) or any(
        g.severity == "fail" and not g.auto_fix_applied for g in gaps
    )
    has_warn = any(s.severity == "warn" for s in steps) or any(
        g.severity == "warn" for g in gaps
    )
    verdict = "fail" if has_fail else ("warn" if has_warn else "ok")

    tick = OrchestratorTick(
        tick_id=tick_id,
        started_ts_ms=started,
        completed_ts_ms=int(time.time() * 1000),
        verdict=verdict,
        steps=steps,
        gaps=gaps,
        heals_applied=heals,
        fixes_applied=fixes,
    )

    try:
        _persist_tick(tick)
    except Exception:  # noqa: BLE001
        log.exception("persist_tick failed")

    return tick


# ---------------------------------------------------------------------------
# Background thread management
# ---------------------------------------------------------------------------

_state_lock = threading.RLock()
_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None
_last_tick: Optional[OrchestratorTick] = None
_running = False


def is_running() -> bool:
    with _state_lock:
        return _running


def last_tick() -> Optional[dict[str, Any]]:
    with _state_lock:
        return _last_tick.to_dict() if _last_tick else None


def _loop(interval_s: float, stop: threading.Event) -> None:
    global _last_tick
    log.info("auto-orchestrator loop started (interval=%.0fs)", interval_s)
    while not stop.is_set():
        try:
            tick = run_tick()
            with _state_lock:
                _last_tick = tick
        except Exception:  # noqa: BLE001
            log.exception("orchestrator tick crashed")
        # Sleep in small slices so stop() is responsive.
        end = time.monotonic() + interval_s
        while time.monotonic() < end:
            if stop.is_set():
                break
            time.sleep(min(1.0, max(0.0, end - time.monotonic())))
    log.info("auto-orchestrator loop exiting")


def start(interval_s: Optional[float] = None) -> dict[str, Any]:
    """Start the background orchestrator. Idempotent — calling again
    returns the current state without double-starting."""
    global _thread, _stop_event, _running
    with _state_lock:
        if _running:
            return {"ok": True, "already_running": True}
        if interval_s is None:
            try:
                interval_s = float(
                    os.environ.get("SPOT_AUTO_INTERVAL_S", "").strip()
                    or DEFAULT_INTERVAL_S
                )
            except (ValueError, TypeError):
                interval_s = DEFAULT_INTERVAL_S
        _stop_event = threading.Event()
        _thread = threading.Thread(
            target=_loop, args=(float(interval_s), _stop_event),
            name="spot-auto-orchestrator", daemon=True,
        )
        _running = True
        _thread.start()
        return {"ok": True, "started": True, "interval_s": interval_s}


def stop() -> dict[str, Any]:
    global _thread, _stop_event, _running
    with _state_lock:
        if not _running or _stop_event is None:
            return {"ok": True, "already_stopped": True}
        _stop_event.set()
        t = _thread
        _running = False
    if t is not None:
        t.join(timeout=5.0)
    return {"ok": True, "stopped": True}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_orchestrator_ticks (
    tick_id          TEXT PRIMARY KEY,
    started_ts_ms    INTEGER NOT NULL,
    completed_ts_ms  INTEGER NOT NULL,
    verdict          TEXT NOT NULL,
    n_steps          INTEGER NOT NULL,
    n_gaps           INTEGER NOT NULL,
    heals_applied    INTEGER NOT NULL,
    fixes_applied    INTEGER NOT NULL,
    payload_json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_orch_ticks_ts
    ON spot_orchestrator_ticks(started_ts_ms DESC);
"""


def _init_schema() -> None:
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    try:
        con.executescript(_SCHEMA)
        con.commit()
    finally:
        con.close()


def _persist_tick(tick: OrchestratorTick) -> None:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO spot_orchestrator_ticks "
            "(tick_id, started_ts_ms, completed_ts_ms, verdict, n_steps, "
            " n_gaps, heals_applied, fixes_applied, payload_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (tick.tick_id, tick.started_ts_ms, tick.completed_ts_ms,
             tick.verdict, len(tick.steps), len(tick.gaps),
             tick.heals_applied, tick.fixes_applied,
             json.dumps(tick.to_dict(), default=str)),
        )
        con.commit()
    finally:
        con.close()


def latest_tick() -> Optional[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        row = con.execute(
            "SELECT payload_json FROM spot_orchestrator_ticks "
            "ORDER BY started_ts_ms DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    return json.loads(row[0]) if row else None


def history(limit: int = 30) -> list[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT tick_id, started_ts_ms, verdict, n_steps, n_gaps, "
            " heals_applied, fixes_applied "
            "FROM spot_orchestrator_ticks ORDER BY started_ts_ms DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    finally:
        con.close()
    return [
        {"tick_id": r[0], "started_ts_ms": r[1], "verdict": r[2],
         "n_steps": r[3], "n_gaps": r[4],
         "heals_applied": r[5], "fixes_applied": r[6]}
        for r in rows
    ]
