"""
Optional Gemini intelligence layer.

Hard rules:
  * Toggleable. Default OFF.
  * Cannot mutate accounting fields (pnl, fees, slippage, equity_curve).
  * Cannot influence determinism: when a backtest is replayed with the
    same seed and the same Gemini toggle/state, the bars + trades must
    be byte-identical. The Gemini call only annotates the *result*.
  * Schema-validated input + output. Unknown fields ignored.
  * On failure (no key, network error, schema mismatch), returns a
    fallback `{"available": False, ...}` block; the engine still
    completes successfully.

Reads GEMINI_API_KEY from the environment. Uses requests if installed;
otherwise falls back to urllib.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any


GEMINI_MODEL = os.environ.get("GEMINI_BACKTEST_MODEL", "gemini-1.5-flash")
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)


REQUEST_SCHEMA = {
    "summary":   {"required": True, "type": dict},
    "scenarios": {"required": True, "type": list},
    "config":    {"required": True, "type": dict},
}

RESPONSE_FIELDS = (
    "regime_label", "narrative", "weakness_signals",
    "edge_decay_warning", "overfitting_symptoms",
    "sensitivity_commentary",
)


def _validate_request(payload: dict) -> tuple[bool, str]:
    for k, v in REQUEST_SCHEMA.items():
        if v["required"] and k not in payload:
            return False, f"missing field: {k}"
        if k in payload and not isinstance(payload[k], v["type"]):
            return False, f"bad type for {k}"
    return True, ""


def _post_json(url: str, body: dict, timeout: float = 20.0) -> dict:
    try:
        import requests
        r = requests.post(url, json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except ImportError:
        import urllib.request
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))


def annotate_run(run_payload: dict, *, api_key: str | None = None) -> dict:
    """Send a result-summary to Gemini for narrative annotation.

    Returns a dict that MUST be stored under run.ai_commentary. Never
    raises — failure is encoded in the dict.
    """
    key = api_key or os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return {
            "available": False,
            "reason": "no_api_key",
            "fields": {f: None for f in RESPONSE_FIELDS},
        }
    ok, err = _validate_request(run_payload)
    if not ok:
        return {
            "available": False,
            "reason": f"schema_violation:{err}",
            "fields": {f: None for f in RESPONSE_FIELDS},
        }
    prompt = (
        "You are a senior quant analyst. Given the backtest summary below, "
        "respond ONLY with strict JSON containing fields: "
        f"{', '.join(RESPONSE_FIELDS)}. Be concise (<=80 words per field).\n\n"
        + json.dumps({
            "summary":   run_payload.get("summary"),
            "scenarios": run_payload.get("scenarios"),
            "config":    run_payload.get("config"),
        }, default=str)[:8000]
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    url = f"{GEMINI_ENDPOINT}?key={key}"
    started = time.time()
    try:
        raw = _post_json(url, body)
        text = (
            raw.get("candidates", [{}])[0]
               .get("content", {})
               .get("parts", [{}])[0]
               .get("text", "")
        )
        parsed = json.loads(text) if text.strip().startswith("{") else {}
        fields = {f: parsed.get(f) for f in RESPONSE_FIELDS}
        return {
            "available": True,
            "reason": "ok",
            "model": GEMINI_MODEL,
            "latency_ms": int((time.time() - started) * 1000),
            "fields": fields,
        }
    except Exception as exc:
        return {
            "available": False,
            "reason": f"transport_error:{type(exc).__name__}",
            "model": GEMINI_MODEL,
            "fields": {f: None for f in RESPONSE_FIELDS},
        }


def _assistant_call(prompt: str, expected_fields: tuple[str, ...], *, api_key: str | None = None) -> dict:
    """Shared transport for all Gemini assistant endpoints.

    Always returns a dict with `available`, `reason`, `fields`. Never raises.
    """
    key = api_key or os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return {
            "available": False,
            "reason": "no_api_key",
            "fields": {f: None for f in expected_fields},
        }
    body = {
        "contents": [{"parts": [{"text": prompt[:8000]}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    url = f"{GEMINI_ENDPOINT}?key={key}"
    started = time.time()
    try:
        raw = _post_json(url, body)
        text = (
            raw.get("candidates", [{}])[0]
               .get("content", {})
               .get("parts", [{}])[0]
               .get("text", "")
        )
        parsed = json.loads(text) if text.strip().startswith("{") else {}
        return {
            "available": True,
            "reason": "ok",
            "model": GEMINI_MODEL,
            "latency_ms": int((time.time() - started) * 1000),
            "fields": {f: parsed.get(f) for f in expected_fields},
        }
    except Exception as exc:
        return {
            "available": False,
            "reason": f"transport_error:{type(exc).__name__}",
            "model": GEMINI_MODEL,
            "fields": {f: None for f in expected_fields},
        }


SYNTH_FIELDS = ("code", "name", "description", "mu_annual", "sigma_daily",
                "jump_prob", "jump_scale", "mean_rev", "funding_bps_8h",
                "trade_freq_bias", "trend_persistence", "rationale")

BLEND_FIELDS = ("picks", "mode", "rationale", "expected_regime",
                "cautions", "recommended_seed_sweep")

ANOMALY_FIELDS = ("anomalies", "severity", "suggested_mitigations",
                  "flagged_bars", "confidence")

REPORT_FIELDS = ("executive_summary", "regime_commentary", "pnl_attribution",
                 "risk_commentary", "sensitivity_commentary",
                 "weakness_signals", "action_items")


def synthesize_scenario(brief: str, *, api_key: str | None = None) -> dict:
    """Gemini designs a new parametric scenario from a natural-language brief.

    Never mutates the on-disk SCENARIOS registry — returns spec fields for
    the operator to review / merge.
    """
    prompt = (
        "You are a senior quant designing a synthetic crypto-regime for a "
        "backtest harness. Respond ONLY as strict JSON with fields: "
        f"{', '.join(SYNTH_FIELDS)}. Numeric fields must be plain floats. "
        "mu_annual ∈ [-3, 3], sigma_daily ∈ [0.005, 0.12], "
        "jump_prob ∈ [0, 0.02], jump_scale ∈ [1, 10], mean_rev ∈ [0, 0.6], "
        "funding_bps_8h ∈ [-40, 40].\n\nBrief:\n"
        + brief
    )
    return _assistant_call(prompt, SYNTH_FIELDS, api_key=api_key)


def suggest_blend(goal: str, *, api_key: str | None = None) -> dict:
    """Gemini recommends a scenario blend (mode + picks + weights) for a goal."""
    prompt = (
        "Given the operator goal below, propose a scenario blend from this "
        "registry of codes: BLACK_SWAN, EUPHORIC_BULL_FLUSH, HIGH_VOL_RANGE, "
        "BEAR_BLEED, SIDEWAYS_CHOP, S1_2021_BULL, S2_2022_BEAR, "
        "S3_2023_RECOVERY, S4_2024_HALVING_ETF, S5_2025_LATE_CYCLE. "
        "Respond ONLY as strict JSON with fields: "
        f"{', '.join(BLEND_FIELDS)}. `picks` must be a list of "
        "{code, weight} objects. `mode` ∈ single|multi|weighted|chained|randomized.\n\n"
        "Goal:\n" + goal
    )
    return _assistant_call(prompt, BLEND_FIELDS, api_key=api_key)


def detect_anomalies(run_payload: dict, *, api_key: str | None = None) -> dict:
    """Gemini inspects a completed run and surfaces suspicious behaviour."""
    body = json.dumps({
        "summary":      run_payload.get("summary"),
        "trades_head":  run_payload.get("trades", [])[:60],
        "attribution":  run_payload.get("attribution"),
        "drawdown_tail": run_payload.get("drawdown_curve", [])[-200:],
    }, default=str)[:8000]
    prompt = (
        "You are a risk auditor. Inspect this backtest for anomalies — "
        "overfit symptoms, edge decay, unexplained PnL spikes, attribution "
        "imbalance, cost blowups. Respond ONLY as strict JSON with fields: "
        f"{', '.join(ANOMALY_FIELDS)}. `anomalies` is a list of strings; "
        "`flagged_bars` is a list of bar indices; severity ∈ low|medium|high.\n\n"
        + body
    )
    return _assistant_call(prompt, ANOMALY_FIELDS, api_key=api_key)


def full_report(run_payload: dict, *, api_key: str | None = None) -> dict:
    """Gemini generates a structured end-of-run report."""
    body = json.dumps({
        "summary":      run_payload.get("summary"),
        "config":       run_payload.get("config"),
        "attribution":  run_payload.get("attribution"),
        "timeline":     run_payload.get("timeline"),
    }, default=str)[:8000]
    prompt = (
        "Write a detailed operator-grade backtest report. Respond ONLY as "
        "strict JSON with fields: "
        f"{', '.join(REPORT_FIELDS)}. Each field ≤ 160 words. "
        "`action_items` is a list of strings.\n\n"
        + body
    )
    return _assistant_call(prompt, REPORT_FIELDS, api_key=api_key)
