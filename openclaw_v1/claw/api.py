"""
Claw FastAPI surface — read-mostly. Mounted under /claw/*.

Endpoints are deliberately narrow — Claw's job is to surface truth, not to
let operators reshape it. Nothing here can mutate bot_decisions_immutable.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from . import (
    CLAW_NIC_VERSION,
    commentary,
    execution_tracker,
    fetch_bot_decision,
    init_claw_schema,
    list_bot_decisions,
    list_incidents,
    llm,
    notify,
    record_bot_decision,
    watchdog,
)


router = APIRouter(prefix="/claw", tags=["claw"])


# Ensure schema is present the moment the router is mounted.
init_claw_schema()


@router.get("/contract")
def get_contract() -> dict[str, Any]:
    """Return the current non-interference contract definition."""
    from .contract import BOT_FIELDS_FROZEN
    return {
        "nic_version": CLAW_NIC_VERSION,
        "frozen_fields": list(BOT_FIELDS_FROZEN),
        "guarantees": [
            "Claw never modifies bot scores, confidence, or plans.",
            "bot_decisions_immutable is append-only (DB triggers enforce UPDATE/DELETE denial).",
            "Every bot payload is hashed over BOT_FIELDS_FROZEN on ingest.",
            "Commentary is tagged source=claw.commentary / authoritative=false.",
            "Watchdog may only self-heal infrastructure, never strategy.",
        ],
    }


# ---------------------------------------------------------------------------
# Plane A — bot decisions (read-only, append-only on POST)
# ---------------------------------------------------------------------------

@router.get("/bot-decisions")
def get_bot_decisions(
    strategy_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    return {"rows": list_bot_decisions(strategy_id=strategy_id, limit=limit)}


@router.get("/bot-decisions/{row_id}")
def get_bot_decision(row_id: int) -> dict[str, Any]:
    try:
        return fetch_bot_decision(row_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="bot decision not found")


@router.post("/ingest/bot-decision")
def post_ingest_bot_decision(payload: dict = Body(...)) -> dict[str, Any]:
    """Write-only endpoint — records a bot payload verbatim. Idempotent."""
    try:
        return record_bot_decision(
            strategy_id=str(payload.get("strategy_id") or ""),
            payload=payload.get("payload") or {},
            ingest_source=str(payload.get("ingest_source") or "http"),
            symbol=payload.get("symbol"),
            cycle_id=payload.get("cycle_id"),
            signature=payload.get("signature"),
            correlation_id=payload.get("correlation_id"),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ---------------------------------------------------------------------------
# Plane B — claw execution facts
# ---------------------------------------------------------------------------

@router.get("/executions")
def get_executions(
    state: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    return {"rows": execution_tracker.list_executions(
        state=state, symbol=symbol, limit=limit,
    )}


@router.get("/executions/open")
def get_executions_open() -> dict[str, Any]:
    return {"rows": execution_tracker.list_open_executions()}


@router.get("/executions/{execution_id}")
def get_execution(execution_id: int) -> dict[str, Any]:
    try:
        row = execution_tracker.get_execution(execution_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="execution not found")
    row["events"] = execution_tracker.list_events(execution_id)
    return row


# ---------------------------------------------------------------------------
# Health, incidents, probes
# ---------------------------------------------------------------------------

@router.get("/health")
def get_health(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    probes = watchdog.list_recent_probes(limit=limit)
    latest_by_probe: dict[str, dict[str, Any]] = {}
    for p in probes:
        name = p["probe"]
        if name not in latest_by_probe:
            latest_by_probe[name] = p
    unresolved = list_incidents(unresolved_only=True, limit=50)
    return {
        "nic_version": CLAW_NIC_VERSION,
        "latest_by_probe": latest_by_probe,
        "recent_probes": probes,
        "unresolved_incidents": unresolved,
    }


@router.get("/incidents")
def get_incidents(
    unresolved_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    return {"rows": list_incidents(unresolved_only=unresolved_only, limit=limit)}


@router.post("/watchdog/run")
def post_watchdog_run() -> dict[str, Any]:
    """Execute the default infra probes once and return the report."""
    from .watchdog import probe_clock, probe_db, probe_disk, run_probes
    report = run_probes([probe_db, probe_disk, probe_clock])
    return {
        "nic_version": CLAW_NIC_VERSION,
        "probes": [
            {
                "probe": p.probe, "ok": p.ok,
                "latency_ms": p.latency_ms, "detail": p.detail,
                "auto_action": p.auto_action,
            }
            for p in report.probes
        ],
        "incidents_opened": report.incidents_opened,
    }


# ---------------------------------------------------------------------------
# Commentary (tagged non-authoritative)
# ---------------------------------------------------------------------------

@router.post("/commentary/scenario")
def post_commentary_scenario(payload: dict = Body(...)) -> dict[str, Any]:
    brief = str(payload.get("brief") or "").strip()
    if not brief:
        raise HTTPException(status_code=422, detail="brief required")
    return commentary.synthesize_scenario(brief)


@router.post("/commentary/blend")
def post_commentary_blend(payload: dict = Body(...)) -> dict[str, Any]:
    goal = str(payload.get("goal") or "").strip()
    if not goal:
        raise HTTPException(status_code=422, detail="goal required")
    return commentary.suggest_blend(goal)


@router.post("/commentary/anomalies")
def post_commentary_anomalies(payload: dict = Body(...)) -> dict[str, Any]:
    return commentary.detect_anomalies(payload.get("run") or {})


@router.post("/commentary/report")
def post_commentary_report(payload: dict = Body(...)) -> dict[str, Any]:
    return commentary.full_report(payload.get("run") or {})


# ---------------------------------------------------------------------------
# AI endpoints removed — will be re-added under /apex/consensus in Phase 2
# (APEX-Ω 5-LLM Bayesian consensus + Opus veto).
# ---------------------------------------------------------------------------


@router.post("/commentary/narrate")
def post_commentary_narrate(payload: dict = Body(...)) -> dict[str, Any]:
    """Generic prompt → commentary via any configured provider.

    Body: {"prompt": str, "provider": "auto"|"openai"|"anthropic"|"gemini"|"mistral"|"openrouter"}
    """
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=422, detail="prompt required")
    provider = str(payload.get("provider") or "auto")
    return commentary.narrate(prompt, provider=provider)


# ---------------------------------------------------------------------------
# LLM provider metadata + Notify channels
# ---------------------------------------------------------------------------

@router.get("/llm/providers")
def get_llm_providers() -> dict[str, Any]:
    return {
        "available": llm.available_providers(),
        "default_models": llm.PROVIDER_MODELS,
        "source": "claw.llm",
        "authoritative": False,
    }


@router.get("/notify/channels")
def get_notify_channels() -> dict[str, Any]:
    return {
        "configured": notify.configured_channels(),
        "severities": list(notify.SEVERITIES),
    }


@router.post("/notify/test")
def post_notify_test(payload: dict = Body(default={})) -> dict[str, Any]:
    """Fire a test notification to every configured channel."""
    text = str(payload.get("text") or "claw247 notify test").strip()
    severity = str(payload.get("severity") or "info")
    results = notify.notify(text, severity=severity, tag="claw.api")
    return {
        "channels_attempted": [r.channel for r in results],
        "results": [
            {"channel": r.channel, "ok": r.ok, "detail": r.detail}
            for r in results
        ],
    }
