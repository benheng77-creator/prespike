"""
Read-only FastAPI shim for the claw247 dashboard.

Exposes /status, /positions, /trades, /gate backed by the same SQLite file
TradeLogger writes (core/persistence.py). Deployable standalone; does NOT run
the trader — run main.py alongside it or point TRADE_DB_PATH at a shared
volume.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# Load .env BEFORE anything else reads os.environ. Search order:
#   1. $OPENCLAW_ENV_FILE (explicit override)
#   2. <repo-root>/.env
#   3. openclaw_v1/config/.env  (matches main.py convention)
#   4. openclaw_v1/.env
# All are optional; missing files are skipped silently.
try:
    from dotenv import load_dotenv as _load_dotenv
    _here = Path(__file__).resolve().parent
    _candidates = [
        os.environ.get("OPENCLAW_ENV_FILE"),
        str(_here.parent / ".env"),
        str(_here / "config" / ".env"),
        str(_here / ".env"),
    ]
    for _c in _candidates:
        if _c and Path(_c).exists():
            _load_dotenv(_c, override=False)
except ImportError:
    # python-dotenv not installed — env vars must be set in the shell.
    pass


def _flag(name: str) -> bool:
    """True only if the env var is set to an explicitly truthy value (1/true/yes).

    "0", "false", "no", or empty string all return False. This prevents the
    common Python bug where bool("0") == True.
    """
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from scoring import score_from_mapping

DB_PATH = os.environ.get("TRADE_DB_PATH", "trades.db")
MODE = os.environ.get("CLAW_MODE", "paper")
VERSION = os.environ.get("CLAW_VERSION", "dev")
CORS_ORIGINS = os.environ.get("CLAW_CORS_ORIGINS", "*").split(",")

_started_at = time.time()

app = FastAPI(title="claw247-trading", version=VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in CORS_ORIGINS if o.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

try:
    from backtest_plus.api import router as _backtest_plus_router
    app.include_router(_backtest_plus_router)
except Exception as _bt_exc:                          # pragma: no cover - defensive
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "backtest_plus router not mounted: %s", _bt_exc,
    )

# CLAW-NIC-v1 — mount claw audit / execution-tracking / watchdog endpoints.
try:
    from claw.api import router as _claw_router
    app.include_router(_claw_router)
    import logging as _logging
    _logging.getLogger(__name__).info(
        "CLAW-NIC-v1 mounted: bot outputs are frozen at ingest; claw never "
        "modifies bot scores, confidence, or plans."
    )
except Exception as _claw_exc:                        # pragma: no cover - defensive
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "claw router not mounted: %s", _claw_exc,
    )

# Phase 11n-9-q — shared ops infrastructure (PnL, trades, consensus,
# LLM health/cost, notifications, kill, governor, watchdog, halt/pause/
# resume). Mounted at /spot_aggro/ops/*.
try:
    from spot_aggro.ops.routes_ops import router as _ops_router
    app.include_router(_ops_router)
    import logging as _logging
    _logging.getLogger(__name__).info(
        "SPOT AGGRO ops router mounted at /spot_aggro/ops/*"
    )
except Exception as _ops_exc:                         # pragma: no cover
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "SPOT AGGRO ops router NOT mounted: %s", _ops_exc,
    )

# SPOT AGGRO owns its own engine router under /spot_aggro (separate
# mount from /spot_aggro/ops/* shared-ops infra above). Kept separate so
# a failure in one router does not cascade into the other.
try:
    from spot_aggro.api.routes import router as _spot_router
    app.include_router(_spot_router)
    import logging as _logging
    _logging.getLogger(__name__).info("SPOT AGGRO router mounted at /spot_aggro/*")
except Exception as _spot_exc:                        # pragma: no cover
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "SPOT AGGRO router NOT mounted: %s", _spot_exc,
    )


# Phase 11n-9-ss — Contrarian + Deep Value scoped panel router.
# Strictly isolated: mounts at /strategy/contrarian_deepvalue/*,
# feature-flag gated (FEATURE_CONTRARIAN_DEEPVALUE_PANEL), returns
# only CDV-scoped data. Failure here MUST NOT affect existing routers.
try:
    from spot_aggro.api.routes_strategy_cdv import router as _cdv_router
    app.include_router(_cdv_router)
    import logging as _logging
    _logging.getLogger(__name__).info(
        "CDV panel router mounted at /strategy/contrarian_deepvalue/*",
    )
except Exception as _cdv_exc:                         # pragma: no cover
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "CDV panel router NOT mounted: %s", _cdv_exc,
    )


# Phase 11e — Path A: serve the operator dashboard directly from uvicorn.
#
# Before this, the dashboard only existed at https://claw247-trading.pages.dev/ops/
# (Cloudflare Pages deployment), which was serving an orphan 52 KB HTML that
# matched no branch in this repo and never picked up any Phase 11 work.
# Operators viewing that URL saw a stale, contradictory page for 12+ hours
# while the actual fixes were sitting on disk.
#
# Mounting web/ops/ directly bypasses the entire deploy pipeline for the
# local case — `http://127.0.0.1:8080/ops/` now serves the current disk
# HTML (Phase 11d, with every truth-coherence, auth, heatmap, and card
# cleanup patch applied). Zero Cloudflare dependency for the local operator.
try:
    from fastapi.staticfiles import StaticFiles
    _ops_dir = Path(__file__).resolve().parent.parent / "web" / "ops"
    if _ops_dir.exists() and (_ops_dir / "index.html").exists():
        # html=True makes / resolve to index.html; also serves assets
        # (openapi.json, etc.) side-by-side.
        app.mount("/ops", StaticFiles(directory=str(_ops_dir), html=True), name="ops")
        import logging as _logging
        _logging.getLogger(__name__).info(
            "SPOT AGGRO dashboard mounted at /ops/ (live from %s)", _ops_dir,
        )
    else:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "web/ops/ not found at %s — dashboard not served locally", _ops_dir,
        )
except Exception as _ops_exc:                         # pragma: no cover
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "dashboard mount failed: %s", _ops_exc,
    )


# Phase 11 — SPOT AGGRO telemetry (QuestDB + Grafana + Sentry).
# Non-blocking. If QuestDB is unreachable, emits silently drop.
# Sentry stays dormant until SPOT_SENTRY_BACKEND_DSN is set.
@app.on_event("startup")
async def _spot_telemetry_startup() -> None:
    try:
        from spot_aggro.telemetry import init_telemetry
        init_telemetry()
        import logging as _logging
        _logging.getLogger(__name__).info(
            "spot_aggro telemetry initialised (non-blocking)"
        )
    except Exception as exc:  # noqa: BLE001
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "spot_aggro telemetry init failed (engine continues): %s", exc
        )


@app.on_event("shutdown")
async def _spot_telemetry_shutdown() -> None:
    try:
        from spot_aggro.telemetry import shutdown_telemetry
        shutdown_telemetry()
    except Exception:  # noqa: BLE001
        pass


# Phase 11h — SPOT AGGRO engine MUST NOT auto-start on server boot.
#
# A prior implementation added a SPOT_AGGRO_AUTO_ARM gate here. It was
# removed because even with the flag off by default, its mere presence
# turned uvicorn boot into a live-trading surface: one stray env var
# from a shell, a Docker compose file, or a systemd unit could silently
# place real orders during a server restart. An incident on
# 2026-04-19 confirmed the blast radius — an inadvertent
# SPOT_AGGRO_AUTO_ARM=1 caused 5 live Tier C entries on OKX before the
# operator could react.
#
# Policy going forward: the engine is started ONLY by the operator
# clicking Resume on the dashboard (POST /spot_aggro/start with a valid
# OPS_ADMIN_TOKEN). There is no server-side auto-start, no env-gated
# auto-start, and no startup hook that instantiates the trading engine.
# This keeps uvicorn boot purely a read-only-plus-control-plane action.


@app.on_event("startup")
def _start_ops_background_services() -> None:
    """Start shared-ops schedulers: 3h PnL report + notifications ready."""
    import logging as _logging
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.ops.notifications import router as _nr  # noqa: F401
        _log.info("ops notifications router primed")
    except Exception as exc:
        _log.warning("ops notifications router NOT primed: %s", exc)
    try:
        from spot_aggro.ops.scheduler import pnl_reporter
        pnl_reporter.start()
        _log.info("ops PnL reporter started (3h interval)")
    except Exception as exc:                          # pragma: no cover
        _log.warning("ops PnL reporter NOT started: %s", exc)
    try:
        from spot_aggro.ops.watchdog import llm_watchdog
        llm_watchdog.start(interval_s=300)
        _log.info("ops LLM watchdog started (5min interval)")
    except Exception as exc:                          # pragma: no cover
        _log.warning("ops watchdog NOT started: %s", exc)
    try:
        from spot_aggro.ops.publisher import cloud_sync
        cloud_sync.start()
    except Exception as exc:                          # pragma: no cover
        _log.warning("ops cloud publisher NOT started: %s", exc)


# Phase 11j — daily SPOT AGGRO system-integrity audit.
#
# Runs once per day on a detached daemon thread. No APScheduler dep; uses
# a simple repeat-timer so the scheduler can't fail import. First run
# happens 60s after boot so the engine has time to warm up. Subsequent
# runs every 24h. Each run stores its result in spot_system_audit_runs
# and the dashboard's System Audit card shows the latest verdict.
#
# The audit is READ-ONLY over trading state — it never starts the engine,
# never places orders, never consults capital. It only verifies that
# every piece needed to produce a trading decision is in place, connected,
# non-broken, and fully functional.
# Phase 11l — hourly SPOT AGGRO win-rate research agent.
#
# Runs the research agent every hour on a detached daemon thread. The
# agent reads closed-exit stats from the trade log, computes per-tier
# win rate, soft-halts any tier with WR < WR_HALT_MIN (default 60%) on
# a sufficient sample, and thaws any tier whose WR has recovered past
# WR_RESUME_MIN (default 50%) — operator-lockable hysteresis.
#
# Read-only against trading state except for the tier-toggle flip, which
# is the explicit execution-lane control surface the research agent is
# authorised to use.
@app.on_event("startup")
def _start_spot_aggro_research_agent() -> None:
    import logging as _logging
    import threading
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.governance.research_agent import run_and_persist
    except Exception as exc:  # noqa: BLE001
        _log.warning("spot_aggro research agent NOT started (import): %s", exc)
        return

    _INTERVAL_S = 3600                 # 1h
    _FIRST_DELAY_S = 30                # run quickly so halt decisions apply fast

    def _tick() -> None:
        try:
            r = run_and_persist(window_h=24)
            halted = [t for t, h in (r.halt_state or {}).items() if h]
            _log.info(
                "[spot_aggro.research] %s WR=%s exits=%d halted=%s",
                r.report_id,
                (f"{r.overall_wr*100:.1f}%" if r.overall_wr is not None else "n/a"),
                r.overall_exits,
                halted or "none",
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("spot_aggro research agent tick failed: %s", exc)
        finally:
            t = threading.Timer(_INTERVAL_S, _tick)
            t.daemon = True
            t.start()

    first = threading.Timer(_FIRST_DELAY_S, _tick)
    first.daemon = True
    first.start()
    _log.info(
        "spot_aggro research agent scheduled (first run in %ds, then every 1h)",
        _FIRST_DELAY_S,
    )


@app.on_event("startup")
def _start_spot_aggro_daily_auditor() -> None:
    import logging as _logging
    import threading
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.governance.daily_system_auditor import run_and_persist
    except Exception as exc:  # noqa: BLE001
        _log.warning("spot_aggro daily auditor NOT started (import): %s", exc)
        return

    _INTERVAL_S = 24 * 3600       # 24h
    _FIRST_DELAY_S = 60           # let the server finish booting before first run

    def _tick() -> None:
        try:
            r = run_and_persist()
            _log.info(
                "[spot_aggro.daily_audit] %s verdict=%s ok=%d warn=%d fail=%d",
                r.run_id, r.verdict, r.n_ok, r.n_warn, r.n_fail,
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("spot_aggro daily auditor tick failed: %s", exc)
        finally:
            t = threading.Timer(_INTERVAL_S, _tick)
            t.daemon = True
            t.start()

    first = threading.Timer(_FIRST_DELAY_S, _tick)
    first.daemon = True
    first.start()
    _log.info(
        "spot_aggro daily system auditor scheduled (first run in %ds, then every 24h)",
        _FIRST_DELAY_S,
    )


# Phase 11n-9-s — Apex Purge Governor (Layer 10).
#
# Permanent trip-wire that scans for any reintroduction of the purged
# apex_omega package or /apex/ URL surface. Runs once at boot (5s
# delay) then every 5 minutes. When LEGACY_PURGE_GOV_AUTOKILL=1 is set
# (default in the startup env), stray filesystem artifacts (legacy
# apex directories + logs + config files) are deleted automatically.
# Source-code regressions are flagged verdict=fail but never auto-
# patched — a human must review + fix the code.
@app.on_event("startup")
def _start_spot_aggro_legacy_purge_gov() -> None:
    import logging as _logging
    import threading
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.governance.legacy_purge_gov import run_once
    except Exception as exc:  # noqa: BLE001
        _log.warning("legacy_purge_gov NOT started (import): %s", exc)
        return
    # Default AUTOKILL on in the live server process. Tests import the
    # module directly and pass purge=False explicitly, so this env-level
    # default does not affect them.
    os.environ.setdefault("LEGACY_PURGE_GOV_AUTOKILL", "1")

    _INTERVAL_S = 300             # 5 minutes
    _FIRST_DELAY_S = 5

    def _tick() -> None:
        try:
            r = run_once()
            _log.info(
                "[spot_aggro.legacy_purge_gov] verdict=%s stray=%d regressions=%d purged=%d",
                r.verdict, r.n_stray_paths, r.n_src_regressions, r.n_purged,
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("legacy_purge_gov tick failed: %s", exc)
        finally:
            t = threading.Timer(_INTERVAL_S, _tick)
            t.daemon = True
            t.start()

    first = threading.Timer(_FIRST_DELAY_S, _tick)
    first.daemon = True
    first.start()
    _log.info(
        "spot_aggro legacy_purge_gov scheduled (first run in %ds, then every %ds)",
        _FIRST_DELAY_S, _INTERVAL_S,
    )


# Phase 11n-9-t — Apex Deep Forensic Governor (Layer 11).
# Superset of Layer 10: scans configs (YAML/JSON/shell/.env), string
# literals, and the process env for every apex variant. Runs on boot
# (7s delay so Layer 10 fires first) + every 10 minutes.
@app.on_event("startup")
def _start_spot_aggro_legacy_deep_forensic_gov() -> None:
    import logging as _logging
    import threading
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.governance.legacy_deep_forensic_gov import run_once
    except Exception as exc:  # noqa: BLE001
        _log.warning("legacy_deep_forensic_gov NOT started (import): %s", exc)
        return

    _INTERVAL_S = 600             # 10 minutes
    _FIRST_DELAY_S = 7

    def _tick() -> None:
        try:
            r = run_once()
            _log.info(
                "[spot_aggro.legacy_deep_forensic_gov] verdict=%s"
                " stray=%d src=%d config=%d env=%d info_db=%d purged=%d",
                r.verdict, r.n_stray_paths, r.n_src_strings,
                r.n_config, r.n_env, r.n_info, r.n_purged,
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("legacy_deep_forensic_gov tick failed: %s", exc)
        finally:
            t = threading.Timer(_INTERVAL_S, _tick)
            t.daemon = True
            t.start()

    first = threading.Timer(_FIRST_DELAY_S, _tick)
    first.daemon = True
    first.start()
    _log.info(
        "spot_aggro legacy_deep_forensic_gov scheduled (first run in %ds, then every %ds)",
        _FIRST_DELAY_S, _INTERVAL_S,
    )


# Phase 11n-9-y — Layer 12: Economic Truth Governor.
# Computes Wilson-bounded expectancy per (symbol, tier, module, cell)
# every 5 min. Non-enforcing: writes verdicts to
# spot_economic_truth_verdicts. Layer 13 + contradiction freeze read it.
@app.on_event("startup")
def _start_spot_aggro_economic_truth_gov() -> None:
    import logging as _logging
    import threading
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.governance.economic_truth_gov import run_once
    except Exception as exc:  # noqa: BLE001
        _log.warning("economic_truth_gov NOT started (import): %s", exc)
        return

    _INTERVAL_S = 300
    _FIRST_DELAY_S = 8

    def _tick() -> None:
        try:
            r = run_once(window=500)
            _log.info(
                "[spot_aggro.economic_truth] verdict=%s ok=%d warn=%d fail=%d"
                " insufficient=%d exits=%d",
                r.overall_verdict, r.n_cells_ok, r.n_cells_warn,
                r.n_cells_fail, r.n_cells_insufficient, r.window_n_exits,
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("economic_truth_gov tick failed: %s", exc)
        finally:
            t = threading.Timer(_INTERVAL_S, _tick)
            t.daemon = True
            t.start()

    first = threading.Timer(_FIRST_DELAY_S, _tick)
    first.daemon = True
    first.start()
    _log.info(
        "spot_aggro economic_truth_gov scheduled (first run in %ds, then every %ds)",
        _FIRST_DELAY_S, _INTERVAL_S,
    )


# Phase 11n-9-y — Layer 3 active: Contradiction Freeze.
# Every 5 min: compute tech_score / econ_score / contradiction_index
# and evaluate triggers T1..T6. If freeze is active, is_entry_frozen()
# returns True and engine entry paths abort every new trade. Operator
# acknowledgment via POST /spot_aggro/gov/contradiction_freeze/ack
# (verbatim primary_cause typed back).
@app.on_event("startup")
def _start_spot_aggro_contradiction_freeze() -> None:
    import logging as _logging
    import threading
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.governance.contradiction_freeze import tick
    except Exception as exc:  # noqa: BLE001
        _log.warning("contradiction_freeze NOT started (import): %s", exc)
        return

    _INTERVAL_S = 300
    _FIRST_DELAY_S = 10

    def _tick() -> None:
        try:
            ev = tick()
            _log.info(
                "[spot_aggro.contradiction_freeze] tech=%.2f econ=%.2f"
                " ci=%.2f frozen=%s triggers=%s",
                ev.tech_score, ev.econ_score, ev.contradiction_index,
                ev.entry_freeze,
                [t["id"] for t in ev.triggers_fired] or "none",
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("contradiction_freeze tick failed: %s", exc)
        finally:
            t = threading.Timer(_INTERVAL_S, _tick)
            t.daemon = True
            t.start()

    first = threading.Timer(_FIRST_DELAY_S, _tick)
    first.daemon = True
    first.start()
    _log.info(
        "spot_aggro contradiction_freeze scheduled (first run in %ds, then every %ds)",
        _FIRST_DELAY_S, _INTERVAL_S,
    )


# Phase 11n-9-z — Layer 2: Decision Quality Governor.
# Runs every 5 min. Computes Spearman ρ(score, net_pnl) + decile
# expectancy per (tier, regime, bucket) cell. Registers T5
# contradiction-freeze trigger when the same cell is inverted on 3
# consecutive ticks.
@app.on_event("startup")
def _start_spot_aggro_decision_quality_gov() -> None:
    import logging as _logging
    import threading
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.governance.decision_quality_gov import run_once
    except Exception as exc:  # noqa: BLE001
        _log.warning("decision_quality_gov NOT started (import): %s", exc)
        return

    _INTERVAL_S = 300
    _FIRST_DELAY_S = 12

    def _tick() -> None:
        try:
            v = run_once()
            _log.info(
                "[spot_aggro.decision_quality] verdict=%s cells=%d"
                " healthy=%d warn=%d inverted=%d insufficient=%d",
                v.overall_verdict, v.n_cells, v.n_cells_healthy,
                v.n_cells_warn, v.n_cells_inverted, v.n_cells_insufficient,
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("decision_quality_gov tick failed: %s", exc)
        finally:
            t = threading.Timer(_INTERVAL_S, _tick)
            t.daemon = True
            t.start()

    first = threading.Timer(_FIRST_DELAY_S, _tick)
    first.daemon = True
    first.start()
    _log.info(
        "spot_aggro decision_quality_gov scheduled (first run in %ds, then every %ds)",
        _FIRST_DELAY_S, _INTERVAL_S,
    )


# Phase 11n-9-z — Card-Truth Mismatch Detector (M1-M7).
# Runs every 5 min. Registers T4 contradiction-freeze trigger when
# >= 2 mismatches fire.
@app.on_event("startup")
def _start_spot_aggro_card_truth_mismatch() -> None:
    import logging as _logging
    import threading
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.governance.card_truth_mismatch import run_once
    except Exception as exc:  # noqa: BLE001
        _log.warning("card_truth_mismatch NOT started (import): %s", exc)
        return

    _INTERVAL_S = 300
    _FIRST_DELAY_S = 14

    def _tick() -> None:
        try:
            s = run_once()
            if s.findings:
                _log.warning(
                    "[spot_aggro.card_truth_mismatch] %d findings: %s",
                    len(s.findings),
                    [f.rule_id for f in s.findings],
                )
        except Exception as exc:  # noqa: BLE001
            _log.exception("card_truth_mismatch tick failed: %s", exc)
        finally:
            t = threading.Timer(_INTERVAL_S, _tick)
            t.daemon = True
            t.start()

    first = threading.Timer(_FIRST_DELAY_S, _tick)
    first.daemon = True
    first.start()
    _log.info(
        "spot_aggro card_truth_mismatch scheduled (first run in %ds, then every %ds)",
        _FIRST_DELAY_S, _INTERVAL_S,
    )


# Phase 11n-9-z — Shadow Scorer comparison runner.
# Runs every 30 min (lower cadence than decision-quality; each run
# matches all shadow authzs with live exits, O(n log n)).
@app.on_event("startup")
def _start_spot_aggro_shadow_scorer() -> None:
    import logging as _logging
    import threading
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.governance.shadow_scorer import run_comparison
    except Exception as exc:  # noqa: BLE001
        _log.warning("shadow_scorer NOT started (import): %s", exc)
        return

    _INTERVAL_S = 1800
    _FIRST_DELAY_S = 20

    def _tick() -> None:
        try:
            ab = run_comparison()
            _log.info(
                "[spot_aggro.shadow_scorer] verdict=%s A.n=%d B.n=%d"
                " A.exp=%+.4f B.exp=%+.4f reason=%s",
                ab.promotion_verdict, ab.window_n_a, ab.window_n_b,
                ab.a_expectancy, ab.b_expectancy, ab.reason[:80],
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("shadow_scorer tick failed: %s", exc)
        finally:
            t = threading.Timer(_INTERVAL_S, _tick)
            t.daemon = True
            t.start()

    first = threading.Timer(_FIRST_DELAY_S, _tick)
    first.daemon = True
    first.start()
    _log.info(
        "spot_aggro shadow_scorer scheduled (first run in %ds, then every %ds)",
        _FIRST_DELAY_S, _INTERVAL_S,
    )


# Phase 11n-9-aa — Universe Gatekeeper (step 14 enforcement).
# Every 5 min: refresh Layer 1 cell stats, auto-deprecate admitted
# cells with Wilson_upper < 0 on ≥ 50 exits, auto-admit candidate
# cells with Wilson_lower > 0 on ≥ 50 exits.
@app.on_event("startup")
def _start_spot_aggro_universe_gatekeeper() -> None:
    import logging as _logging
    import threading
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.governance.universe_gatekeeper import run_tick
    except Exception as exc:  # noqa: BLE001
        _log.warning("universe_gatekeeper NOT started (import): %s", exc)
        return

    _INTERVAL_S = 300
    _FIRST_DELAY_S = 16

    def _tick() -> None:
        try:
            t = run_tick()
            n_adm = len(t.admitted)
            n_new = len(t.newly_admitted)
            n_dep = len(t.newly_deprecated)
            _log.info(
                "[spot_aggro.universe_gatekeeper] admitted=%d"
                " newly_admitted=%d newly_deprecated=%d",
                n_adm, n_new, n_dep,
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("universe_gatekeeper tick failed: %s", exc)
        finally:
            t = threading.Timer(_INTERVAL_S, _tick)
            t.daemon = True
            t.start()

    first = threading.Timer(_FIRST_DELAY_S, _tick)
    first.daemon = True
    first.start()
    _log.info(
        "spot_aggro universe_gatekeeper scheduled (first run in %ds, then every %ds)",
        _FIRST_DELAY_S, _INTERVAL_S,
    )


# Phase 11n-9-aa — Trade Readiness flag daemon (mechanical release).
# Every 60s re-evaluate the 5 conditions. Engine entry paths read the
# cached flag every tick; this daemon keeps the cache fresh.
@app.on_event("startup")
def _start_spot_aggro_trade_readiness() -> None:
    import logging as _logging
    import threading
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.governance.trade_readiness import evaluate
    except Exception as exc:  # noqa: BLE001
        _log.warning("trade_readiness NOT started (import): %s", exc)
        return

    _INTERVAL_S = 60
    _FIRST_DELAY_S = 18

    def _tick() -> None:
        try:
            t = evaluate()
            if t.ready:
                _log.info("[spot_aggro.trade_readiness] READY")
            else:
                _log.info(
                    "[spot_aggro.trade_readiness] NOT_READY unmet=%s",
                    t.unmet,
                )
        except Exception as exc:  # noqa: BLE001
            _log.exception("trade_readiness tick failed: %s", exc)
        finally:
            t = threading.Timer(_INTERVAL_S, _tick)
            t.daemon = True
            t.start()

    first = threading.Timer(_FIRST_DELAY_S, _tick)
    first.daemon = True
    first.start()
    _log.info(
        "spot_aggro trade_readiness scheduled (first run in %ds, then every %ds)",
        _FIRST_DELAY_S, _INTERVAL_S,
    )


# Phase 11n-9-ll: Formula-review brainstorm daemon (6h cadence).
@app.on_event("startup")
def _start_spot_aggro_formula_review() -> None:
    import logging as _logging
    import threading
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.governance.formula_review import run as _fr_run
    except Exception as exc:  # noqa: BLE001
        _log.warning("formula_review NOT started (import): %s", exc)
        return
    _INTERVAL_S = 6 * 3600
    _FIRST_DELAY_S = 120

    def _tick() -> None:
        try:
            v = _fr_run()
            _log.info(
                "[spot_aggro.formula_review] verdict=%s headline=%s",
                v.verdict, (v.headline or "")[:80],
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("formula_review tick failed: %s", exc)
        finally:
            t = threading.Timer(_INTERVAL_S, _tick)
            t.daemon = True
            t.start()

    first = threading.Timer(_FIRST_DELAY_S, _tick)
    first.daemon = True
    first.start()
    _log.info(
        "spot_aggro formula_review scheduled (first in %ds, then every %ds)",
        _FIRST_DELAY_S, _INTERVAL_S,
    )


# Phase 11n-9-ll: Daily 24h governance report daemon.
@app.on_event("startup")
def _start_spot_aggro_daily_report() -> None:
    import logging as _logging
    import threading
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.governance.daily_report import generate as _dr_gen
    except Exception as exc:  # noqa: BLE001
        _log.warning("daily_report NOT started (import): %s", exc)
        return
    _INTERVAL_S = 24 * 3600
    _FIRST_DELAY_S = 300   # 5 min after boot

    def _tick() -> None:
        try:
            r = _dr_gen()
            _log.info(
                "[spot_aggro.daily_report] %s verdict=%s headline=%s",
                r.report_date, r.verdict, (r.headline or "")[:100],
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("daily_report tick failed: %s", exc)
        finally:
            t = threading.Timer(_INTERVAL_S, _tick)
            t.daemon = True
            t.start()

    first = threading.Timer(_FIRST_DELAY_S, _tick)
    first.daemon = True
    first.start()
    _log.info(
        "spot_aggro daily_report scheduled (first in %ds, then every %ds)",
        _FIRST_DELAY_S, _INTERVAL_S,
    )


# Phase 11n-9-gg: Model-registry self-registration on boot.
@app.on_event("startup")
def _start_spot_aggro_model_registry_bootstrap() -> None:
    import logging as _logging
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.governance.model_registry import bootstrap_self_register
        recs = bootstrap_self_register()
        _log.info(
            "spot_aggro model_registry bootstrap: %d model versions registered",
            len(recs),
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("model_registry bootstrap failed: %s", exc)


# Phase 11n-9-ff: Kill-ladder auto-pause daemon. Every 60s evaluates
# the reject-storm detector and auto-releases L1 after cooldown.
# L2/L3/L4 never auto-release — operator-only.
@app.on_event("startup")
def _start_spot_aggro_kill_ladder_daemon() -> None:
    import logging as _logging
    import threading
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.governance.kill_ladder import (
            evaluate_auto_pause as _klp,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("kill_ladder daemon NOT started (import): %s", exc)
        return
    _INTERVAL_S = 60
    _FIRST_DELAY_S = 20

    def _tick() -> None:
        try:
            st = _klp()
            if st.level != "L0":
                _log.warning(
                    "[spot_aggro.kill_ladder] level=%s reason=%s",
                    st.level, st.reason,
                )
        except Exception as exc:  # noqa: BLE001
            _log.exception("kill_ladder tick failed: %s", exc)
        finally:
            t = threading.Timer(_INTERVAL_S, _tick)
            t.daemon = True
            t.start()

    first = threading.Timer(_FIRST_DELAY_S, _tick)
    first.daemon = True
    first.start()
    _log.info("spot_aggro kill_ladder daemon scheduled (every %ds)", _INTERVAL_S)


# Phase 11n-9-ee: Three-way shadow verdict daemon.
# Computes control vs contrarian vs mean_reversion standings every 5
# minutes and persists the verdict to shadow_variant_verdicts. First
# variant to satisfy the promotion rule wins. Read-only; no trades.
@app.on_event("startup")
def _start_spot_aggro_three_way_shadow() -> None:
    import logging as _logging
    import threading
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.governance.three_way_shadow import evaluate as _tw_eval
    except Exception as exc:  # noqa: BLE001
        _log.warning("three_way_shadow NOT started (import): %s", exc)
        return
    _INTERVAL_S = 300
    _FIRST_DELAY_S = 25

    def _tick() -> None:
        try:
            v = _tw_eval()
            _log.info(
                "[spot_aggro.three_way_shadow] leader=%s exits=%d verdict=%s",
                v.leader, v.leader_exits, v.promotion_verdict,
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("three_way_shadow tick failed: %s", exc)
        finally:
            t = threading.Timer(_INTERVAL_S, _tick)
            t.daemon = True
            t.start()

    first = threading.Timer(_FIRST_DELAY_S, _tick)
    first.daemon = True
    first.start()
    _log.info(
        "spot_aggro three_way_shadow scheduled (first run in %ds, then every %ds)",
        _FIRST_DELAY_S, _INTERVAL_S,
    )


# Phase 11n-9-vv: variant trip-wire daemon. Every 5 min evaluates
# per-variant 24h PnL + 40-exit promotion status. Auto-disables any
# variant whose 24h net PnL <= -SPOT_VARIANT_DD_KILL_USD. Records
# promote / permanent_disable verdicts when 40 exits reached.
@app.on_event("startup")
def _start_spot_aggro_variant_trip_wire() -> None:
    import logging as _logging
    import threading
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.governance.variant_trip_wire import evaluate as _vtw_eval
    except Exception as exc:  # noqa: BLE001
        _log.warning("variant_trip_wire NOT started (import): %s", exc)
        return
    _INTERVAL_S = 300
    _FIRST_DELAY_S = 40

    def _tick() -> None:
        try:
            r = _vtw_eval()
            if r.n_disabled or r.n_promoted:
                _log.warning(
                    "[spot_aggro.variant_trip_wire] disabled=%d promoted=%d",
                    r.n_disabled, r.n_promoted,
                )
        except Exception as exc:  # noqa: BLE001
            _log.exception("variant_trip_wire tick failed: %s", exc)
        finally:
            t = threading.Timer(_INTERVAL_S, _tick)
            t.daemon = True
            t.start()

    first = threading.Timer(_FIRST_DELAY_S, _tick)
    first.daemon = True
    first.start()
    _log.info(
        "spot_aggro variant_trip_wire scheduled (first in %ds, then every %ds)",
        _FIRST_DELAY_S, _INTERVAL_S,
    )


# Phase 11n-9-qq: Crypto.com read-only comparison-feed daemon. Fetches
# ticker + top-of-book depth for admitted tier-C symbols every 60s
# from BOTH OKX and Crypto.com public REST. Writes
# `spot_exchange_comparison` rows for governance + dashboard review.
# Never places orders. Never reads balances. Failure on either side is
# non-fatal — partial rows persist.
@app.on_event("startup")
def _start_spot_aggro_exchange_comparison() -> None:
    import logging as _logging
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.ops.scheduler import exchange_comparison_feed
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "spot_aggro exchange_comparison_feed NOT started (import): %s",
            exc,
        )
        return
    try:
        exchange_comparison_feed.start()
        _log.info("spot_aggro exchange_comparison_feed started (60s cadence)")
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "spot_aggro exchange_comparison_feed start failed: %s", exc,
        )


# Phase 11n-9-dd: heartbeat writer keeps equity_marks fresh even while
# the engine is intentionally stopped, so Trading Engine + Account
# Snapshot cards no longer flip to STALE/FAIL when the operator chooses
# to keep the engine off. Daemon-threaded, fail-open, idempotent start.
@app.on_event("startup")
def _start_spot_aggro_heartbeat_writer() -> None:
    import logging as _logging
    _log = _logging.getLogger(__name__)
    try:
        from spot_aggro.ops.scheduler import heartbeat_writer
    except Exception as exc:  # noqa: BLE001
        _log.warning("spot_aggro heartbeat_writer NOT started (import): %s", exc)
        return
    try:
        heartbeat_writer.start()
        _log.info("spot_aggro heartbeat_writer started (60s cadence)")
    except Exception as exc:  # noqa: BLE001
        _log.warning("spot_aggro heartbeat_writer start failed: %s", exc)


# Phase 11n-8: Auto Orchestrator is 100% auto. Default ON. Set
# SPOT_AUTO_ORCHESTRATOR=0 only if you need to disable it (tests do,
# via monkeypatch). On boot we:
#   (1) start the background loop,
#   (2) fire one tick IMMEDIATELY on a daemon thread so the dashboard
#       has real gov verdicts within ~5s of server start instead of
#       waiting for the first scheduled interval (default 300s).
# Read-only. Validates layers, auto-heals stuck loops, surfaces gaps.
# Never trades.
@app.on_event("startup")
def _start_spot_aggro_auto_orchestrator() -> None:
    import logging as _logging
    import threading as _threading
    _log = _logging.getLogger(__name__)
    if os.environ.get("SPOT_AUTO_ORCHESTRATOR", "1").strip() == "0":
        _log.info("spot_aggro auto-orchestrator disabled by "
                  "SPOT_AUTO_ORCHESTRATOR=0")
        return
    try:
        from spot_aggro.governance import auto_orchestrator as _ao
    except Exception as exc:  # noqa: BLE001
        _log.warning("auto-orchestrator NOT started (import): %s", exc)
        return
    try:
        status = _ao.start()
        _log.info("spot_aggro auto-orchestrator: %s", status)
    except Exception as exc:  # noqa: BLE001
        _log.exception("auto-orchestrator start failed: %s", exc)
        return

    # Seed first tick on a daemon thread so the main startup path
    # returns fast. The orchestrator's scheduled loop picks up the
    # cadence after this tick completes.
    def _first_tick():
        try:
            tk = _ao.run_tick()
            _log.info(
                "auto-orchestrator first tick: verdict=%s steps=%d gaps=%d",
                tk.verdict, len(tk.steps), len(tk.gaps),
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("auto-orchestrator first tick failed: %s", exc)
    _threading.Thread(
        target=_first_tick, name="spot-auto-first-tick", daemon=True,
    ).start()


@app.on_event("shutdown")
def _stop_spot_aggro_auto_orchestrator() -> None:
    try:
        from spot_aggro.governance import auto_orchestrator as _ao
        _ao.stop()
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def _db():
    """SQLite connection with WAL + busy_timeout so concurrent readers and
    the engine writer don't block each other."""
    if not os.path.exists(DB_PATH):
        yield None
        return
    con = sqlite3.connect(DB_PATH, timeout=10.0)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=10000")
        yield con
    finally:
        con.close()


def _rows(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    except sqlite3.Error:
        return []


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/status")
def status():
    with _db() as con:
        has_db = con is not None
        decisions = _rows(con, "SELECT COUNT(*) AS n FROM decisions") if has_db else []
        trades = _rows(con, "SELECT COUNT(*) AS n FROM trades") if has_db else []
    return {
        "mode": MODE,
        "version": VERSION,
        "uptime_s": int(time.time() - _started_at),
        "db_path": DB_PATH,
        "db_present": has_db,
        "decisions_logged": decisions[0]["n"] if decisions else 0,
        "trades_logged": trades[0]["n"] if trades else 0,
        "server_time": int(time.time()),
    }


@app.get("/positions")
def positions():
    with _db() as con:
        if con is None:
            return []
        return _rows(
            con,
            "SELECT symbol, direction, size, entry_px, stop_px, target_px, entry_ts_ms "
            "FROM trades WHERE exit_ts_ms IS NULL ORDER BY entry_ts_ms DESC LIMIT 50",
        )


@app.get("/trades")
def trades():
    with _db() as con:
        if con is None:
            return []
        return _rows(
            con,
            "SELECT symbol, direction, size, entry_px, exit_px, exit_reason, pnl_r, pnl_quote, "
            "entry_ts_ms, exit_ts_ms FROM trades "
            "WHERE exit_ts_ms IS NOT NULL ORDER BY exit_ts_ms DESC LIMIT 50",
        )


@app.get("/gate")
def gate():
    with _db() as con:
        if con is None:
            return {"status": "NO_DB", "window_trades": 0, "hits": 0, "accuracy": 0.0, "threshold": 0.75}
        rows = _rows(
            con,
            "SELECT pnl_r FROM trades WHERE exit_ts_ms IS NOT NULL ORDER BY exit_ts_ms DESC LIMIT 128",
        )
    wins = sum(1 for r in rows if (r.get("pnl_r") or 0) > 0)
    n = len(rows)
    acc = wins / n if n else 0.0
    threshold = float(os.environ.get("CLAW_GATE_THRESHOLD", "0.75"))
    return {
        "status": "UNLOCKED" if acc >= threshold and n >= 20 else "LOCKED",
        "window_trades": n,
        "hits": wins,
        "accuracy": round(acc, 4),
        "threshold": threshold,
    }


from fastapi import Body  # noqa: E402


@app.post("/score")
def score_endpoint(payload: dict = Body(...)):
    try:
        return score_from_mapping(payload)
    except KeyError as e:
        raise HTTPException(status_code=422, detail=f"missing field: {e.args[0]}")
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=str(e))


# ---------------------------------------------------------------------------
# OpenClaw / reports routes (additive). Lazy-imported so the existing read-only
# shim boots even if the optional packages are not present on disk.
# ---------------------------------------------------------------------------

def _ledger():
    from audit import AuditLedger  # noqa: WPS433
    return AuditLedger(db_path=DB_PATH)


@app.get("/openclaw/state")
def openclaw_state():
    try:
        l = _ledger()
        recent = l.fetch_recent(limit=20)
        return {
            "ok": True,
            "actions_count": l.count(),
            "recent_actions": recent,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/openclaw/actions")
def openclaw_actions(limit: int = 100, correlation_id: str | None = None):
    try:
        l = _ledger()
        if correlation_id:
            return {"rows": l.fetch_by_correlation(correlation_id)}
        return {"rows": l.fetch_recent(limit=limit)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/reports/today")
def reports_today():
    import os, json, datetime
    date = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    path = os.path.join("reports", "out", f"{date}.json")
    if not os.path.exists(path):
        cache = os.path.join("cache", "reports", "latest.json")
        if os.path.exists(cache):
            with open(cache, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"date": date, "status": "no_report_yet"}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.get("/reports/{date}")
def reports_by_date(date: str):
    import os, json
    path = os.path.join("reports", "out", f"{date}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="report not found")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
# ---------------------------------------------------------------------------
# Admin-gated secrets management (read masked, write raw via .env).
# ---------------------------------------------------------------------------

from fastapi import Header, status as _status  # noqa: E402


def _require_admin(token: str | None) -> None:
    expected = os.environ.get("OPS_ADMIN_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=403,
                            detail="OPS_ADMIN_TOKEN is not set on this host; refusing secret access")
    if (token or "").strip() != expected:
        raise HTTPException(status_code=401, detail="invalid admin token")


def _secrets_store():
    from secrets_store import SecretsStore
    from audit import AuditLedger
    return SecretsStore(ledger=AuditLedger(db_path=DB_PATH))


@app.get("/ops/secrets")
def ops_secrets_list(x_ops_token: str | None = Header(default=None)):
    _require_admin(x_ops_token)
    return {"rows": _secrets_store().list()}


@app.put("/ops/secrets/{name}")
def ops_secrets_set(
    name: str,
    payload: dict = Body(...),
    x_ops_token: str | None = Header(default=None),
):
    _require_admin(x_ops_token)
    value = payload.get("value")
    if value is None:
        raise HTTPException(status_code=422, detail="missing value")
    try:
        _secrets_store().set(name, str(value))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "name": name}


@app.delete("/ops/secrets/{name}")
def ops_secrets_delete(
    name: str,
    x_ops_token: str | None = Header(default=None),
):
    _require_admin(x_ops_token)
    ok = _secrets_store().delete(name)
    return {"ok": ok, "name": name}
@app.get("/engines/status")
def engines_status():
    """Unified status for every engine attached to this host."""
    out: dict[str, Any] = {}
    try:
        from spot_aggro.ops.routes_ops import ops_status as _ops_status
        out["spot_aggro_ops"] = _ops_status()
    except Exception as e:
        out["spot_aggro_ops"] = {"error": str(e)}
    return out


@app.get("/exchanges")
def exchanges_status():
    """Which exchanges are configured on this host."""
    try:
        from core.exchange_factory import summary
        return summary()
    except Exception as e:
        return {"error": str(e), "supported": []}
@app.post("/ops/intervene")
def ops_intervene(
    payload: dict = Body(...),
    x_ops_token: str | None = Header(default=None),
):
    """Operator intervention (pause/resume/flatten/halt/cancel/restart).

    Records to the audit ledger. Delegates to existing gateway/risk if
    orchestrator is attached; otherwise writes an audit row and returns
    `queued=True` so the caller knows the supervisor must pick it up at
    next restart."""
    _require_admin(x_ops_token)
    from audit import AuditLedger
    verb = (payload.get("verb") or "").upper()
    reason = payload.get("reason") or ""
    if verb not in ("PAUSE", "RESUME", "FLATTEN", "CANCEL_ALL", "RESTART", "EMERGENCY_STOP"):
        raise HTTPException(status_code=422, detail=f"unknown verb: {verb}")
    if not reason.strip():
        raise HTTPException(status_code=422, detail="reason required")
    ledger = AuditLedger(db_path=DB_PATH)
    cid = ledger.record(
        kind="escalated",
        phase="operator_intervention",
        verb=verb,
        severity="warn" if verb in ("PAUSE", "CANCEL_ALL") else "critical",
        result={"verb": verb, "reason": reason, "source": "ops_ui"},
    )
    return {"ok": True, "queued": True, "correlation_id": cid, "verb": verb}

