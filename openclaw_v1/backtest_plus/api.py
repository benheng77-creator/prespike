"""
FastAPI router for /backtest/* endpoints.

All endpoints are read-only by default; POST /backtest/run executes a
synthetic in-process backtest (no exchange I/O). This is safe to expose
on the operator dashboard.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from .composer import ScenarioPick
from .engine import BacktestConfig, CostModel, run_backtest
from .gemini_adapter import (
    annotate_run, synthesize_scenario, suggest_blend,
    detect_anomalies, full_report,
)
from .overlays import OverlayConfig
from .presets import (
    list_presets, load_preset, save_preset,
    list_runs, load_run, save_run,
)
from .scenarios import SCENARIOS
from .strategy import build_strategy, list_strategy_profiles


router = APIRouter(prefix="/backtest", tags=["backtest"])


@router.get("/strategies")
def get_strategies() -> dict[str, Any]:
    """List the strategy profiles the UI can select. Plain-English descriptions."""
    return {"profiles": list_strategy_profiles()}


@router.get("/scenarios")
def get_scenarios() -> dict[str, Any]:
    return {
        "scenarios": [
            {
                "code": s.code,
                "name": s.name,
                "description": s.description,
                "mu_annual": s.mu_annual,
                "sigma_daily": s.sigma_daily,
                "jump_prob": s.jump_prob,
                "jump_scale": s.jump_scale,
                "mean_rev": s.mean_rev,
                "funding_bps_8h": s.funding_bps_8h,
                "trade_freq_bias": s.trade_freq_bias,
                "trend_persistence": s.trend_persistence,
            }
            for s in SCENARIOS.values()
        ],
        "modes": ["single", "multi", "weighted", "chained", "randomized"],
        "boundaries": {
            "capital_min": 1.0,
            "capital_max": 10_000_000.0,
            "days_min": 1,
            "days_max": 1095,
        },
    }


def _build_config(payload: dict) -> BacktestConfig:
    try:
        capital = float(payload["capital"])
        days = float(payload["days"])
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"capital/days required: {e}")
    picks_raw = payload.get("picks") or []
    picks: list[ScenarioPick] = []
    for p in picks_raw:
        if isinstance(p, str):
            picks.append(ScenarioPick(code=p))
        elif isinstance(p, dict):
            repeat = int(p.get("repeat", 1) or 1)
            repeat = max(1, min(repeat, 32))
            weight = float(p.get("weight", 1.0))
            code = str(p.get("code", ""))
            for _ in range(repeat):
                picks.append(ScenarioPick(code=code, weight=weight))
    if not picks:
        raise HTTPException(status_code=422, detail="picks must contain >= 1 scenario")

    cost = CostModel(**(payload.get("cost") or {}))
    overlays = OverlayConfig(**(payload.get("overlays") or {}))
    strategy_id = str(payload.get("strategy") or "balanced")
    strategy = build_strategy(strategy_id)
    return BacktestConfig(
        capital=capital,
        days=days,
        picks=picks,
        mode=str(payload.get("mode", "single")),
        sec_per_bar=int(payload.get("sec_per_bar", 900)),
        seed=int(payload.get("seed", 42)),
        cost=cost,
        overlays=overlays,
        strategy=strategy,
        start_price=float(payload.get("start_price", 50_000.0)),
        use_gemini=bool(payload.get("use_gemini", False)),
        label=str(payload.get("label", "")),
    )


@router.post("/run")
def post_run(payload: dict = Body(...)) -> dict[str, Any]:
    try:
        cfg = _build_config(payload)
        result = run_backtest(cfg)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if cfg.use_gemini:
        result.ai_commentary = annotate_run({
            "summary": result.summary,
            "scenarios": result.timeline,
            "config": result.config,
        })
    saved = save_run(result)
    body = {
        "id": saved["id"],
        "config": result.config,
        "timeline": result.timeline,
        "summary": result.summary,
        "assumptions": result.assumptions,
        "attribution": result.attribution,
        "trades": result.trades[-200:],
        "trade_count_total": len(result.trades),
        "fills": result.fills[-300:],
        "fill_count_total": len(result.fills),
        "decisions": result.decisions[-300:],
        "decision_count_total": len(result.decisions),
        "equity_curve_sample": _downsample(result.equity_curve, 1000),
        "drawdown_curve_sample": _downsample(result.drawdown_curve, 1000),
        "ai_commentary": result.ai_commentary,
        "seed": result.seed,
    }
    return body


@router.post("/compare")
def post_compare(payload: dict = Body(...)) -> dict[str, Any]:
    runs = payload.get("runs") or []
    if len(runs) < 2:
        raise HTTPException(status_code=422, detail="compare requires >= 2 run configs")
    out = []
    for entry in runs:
        cfg = _build_config(entry)
        r = run_backtest(cfg)
        out.append({
            "label": cfg.label or cfg.mode,
            "summary": r.summary,
            "config": r.config,
            "attribution": r.attribution,
            "equity_curve_sample": _downsample(r.equity_curve, 500),
            "drawdown_curve_sample": _downsample(r.drawdown_curve, 500),
        })
    return {"runs": out}


@router.post("/sensitivity")
def post_sensitivity(payload: dict = Body(...)) -> dict[str, Any]:
    """Sweep one parameter over a list of values and return summary metrics.

    Request body:
        {
          "base": { ...same shape as /backtest/run body... },
          "param": "seed" | "fee_bps" | "slippage_bps" | "leverage"
                  | "latency_bars" | "funding_drag_bps_8h" | "no_fill_prob",
          "values": [ ... ]
        }
    """
    base = payload.get("base") or {}
    param = str(payload.get("param", "seed"))
    values = payload.get("values") or []
    if not isinstance(values, list) or not values:
        raise HTTPException(status_code=422, detail="values must be a non-empty list")
    if len(values) > 32:
        raise HTTPException(status_code=422, detail="values capped at 32 per sweep")

    out = []
    for v in values:
        entry = _deep_copy(base)
        _apply_sweep(entry, param, v)
        try:
            cfg = _build_config(entry)
            r = run_backtest(cfg)
        except ValueError as e:
            out.append({"value": v, "error": str(e)})
            continue
        out.append({
            "value": v,
            "summary": r.summary,
            "drawdown_curve_sample": _downsample(r.drawdown_curve, 200),
        })
    return {"param": param, "results": out}


def _deep_copy(d: dict) -> dict:
    import copy
    return copy.deepcopy(d or {})


def _apply_sweep(entry: dict, param: str, value: Any) -> None:
    cost = entry.setdefault("cost", {})
    overlays = entry.setdefault("overlays", {})
    if param == "seed":
        entry["seed"] = int(value)
    elif param == "fee_bps":
        cost["fee_bps_per_side"] = float(value)
    elif param == "slippage_bps":
        cost["base_slippage_bps"] = float(value)
    elif param == "leverage":
        cost["leverage"] = float(value)
    elif param == "latency_bars":
        overlays["latency_bars"] = int(value)
    elif param == "funding_drag_bps_8h":
        overlays["funding_drag_bps_8h"] = float(value)
    elif param == "no_fill_prob":
        overlays["no_fill_prob"] = float(value)
    elif param == "slippage_mult":
        overlays["slippage_mult"] = float(value)
    else:
        raise HTTPException(status_code=422, detail=f"unknown sweep param: {param}")


@router.get("/runs")
def get_runs(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    return {"runs": list_runs(limit=limit)}


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    try:
        return load_run(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="run not found")


@router.get("/presets")
def get_presets() -> dict[str, Any]:
    return {"presets": list_presets()}


@router.post("/presets")
def post_preset(payload: dict = Body(...)) -> dict[str, Any]:
    name = payload.get("name") or ""
    body = payload.get("payload") or {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="payload must be an object")
    try:
        path = save_preset(name, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "name": name, "path": str(path)}


@router.get("/presets/{name}")
def get_preset(name: str) -> dict[str, Any]:
    try:
        return load_preset(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="preset not found")


@router.post("/gemini/scenario")
def post_gemini_scenario(payload: dict = Body(...)) -> dict[str, Any]:
    brief = str(payload.get("brief") or "").strip()
    if not brief:
        raise HTTPException(status_code=422, detail="brief is required")
    return synthesize_scenario(brief)


@router.post("/gemini/blend")
def post_gemini_blend(payload: dict = Body(...)) -> dict[str, Any]:
    goal = str(payload.get("goal") or "").strip()
    if not goal:
        raise HTTPException(status_code=422, detail="goal is required")
    return suggest_blend(goal)


@router.post("/gemini/anomalies")
def post_gemini_anomalies(payload: dict = Body(...)) -> dict[str, Any]:
    run_id = payload.get("run_id")
    if run_id:
        try:
            run = load_run(str(run_id))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="run not found")
    else:
        run = payload.get("run") or {}
    return detect_anomalies(run)


@router.post("/gemini/report")
def post_gemini_report(payload: dict = Body(...)) -> dict[str, Any]:
    run_id = payload.get("run_id")
    if run_id:
        try:
            run = load_run(str(run_id))
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="run not found")
    else:
        run = payload.get("run") or {}
    return full_report(run)


def _downsample(values: list[float], target: int) -> list[float]:
    n = len(values)
    if n <= target:
        return list(values)
    step = max(1, n // target)
    return [values[i] for i in range(0, n, step)][:target]
