"""Phase 11n-9-m — Label Translator (P5).

Deterministic rewrite of internal technical labels into operator-
readable strings. Per blueprint §10:

  consensus.vetoed   → "Trade blocked — high conflict"
  scheduler.fail     → "Scheduler offline"
  feed.latency       → "Feed delay"
  db.conn_err        → "Database issue"
  order_reject       → "Execution rejected"

Plus additional well-known keys we surface across the dashboard:

  pre_trade_gov_block              → "Trade blocked — pre-trade checklist failed"
  research_truth_invalid           → "Research data flagged invalid"
  card_truth_fail                  → "Card audit mismatch"
  loop_novelty_stuck               → "Research loop stuck on repeat"
  daily_alpha_no_admits            → "No trades approved today"
  engine_halt                      → "Engine halted"
  adapter_down                     → "Exchange adapter offline"
  db_unreachable                   → "Database unreachable"
  execution_failure                → "Order execution failed"
  wr_anomaly                       → "Win-rate anomaly"
  latency_spike                    → "Latency spike"
  stale_data                       → "Stale data"
  audit_warn                       → "System audit warning"
  tp_sell_rejected                 → "Take-profit sell rejected"
  gap_detected                     → "Monitoring gap detected"
  orch_gap:daily_alpha             → "No daily-alpha picks admitted"
  orch_gap:loop_novelty            → "Loop novelty flagged"
  orch_gap:decision_truth          → "Decision audit flagged"
  orch_gap:research_truth          → "Research audit flagged"
  orch_gap:card_truth              → "Card audit flagged"

Also rewrites dotted event names. Unknown labels pass through unchanged
but get capitalized for readability (e.g. "some_new_event" → "Some new
event"). Deterministic, no LLM.

SPOT AGGRO only. Used by alert_center's message formatters + anywhere
the dashboard surfaces raw technical kinds.
"""
from __future__ import annotations

from typing import Any


# Canonical map — literal key → plain-English label.
_LABELS: dict[str, str] = {
    # From blueprint §10 (verbatim).
    "consensus.vetoed":     "Trade blocked — high conflict",
    "scheduler.fail":       "Scheduler offline",
    "feed.latency":         "Feed delay",
    "db.conn_err":          "Database issue",
    "order_reject":         "Execution rejected",
    # Spot-aggro alert kinds (11n severity map).
    "engine_halt":          "Engine halted",
    "engine_crash":         "Engine crashed",
    "adapter_down":         "Exchange adapter offline",
    "db_unreachable":       "Database unreachable",
    "execution_failure":    "Order execution failed",
    "account_access_denied": "Account access denied",
    "wr_anomaly":           "Win-rate anomaly",
    "repeated_veto":        "Repeated trade veto",
    "research_truth_invalid": "Research data flagged invalid",
    "card_truth_fail":      "Card audit mismatch",
    "decision_truth_invalid": "Decision audit flagged invalid",
    "pre_trade_gov_block":  "Trade blocked — pre-trade checklist failed",
    "pre_trade_gov_block_repeated": "Trade repeatedly blocked",
    "loop_novelty_stuck":   "Research loop stuck on repeat",
    "tp_sell_rejected":     "Take-profit sell rejected",
    "latency_spike":        "Latency spike",
    "stale_data":           "Stale data",
    "audit_warn":           "System audit warning",
    "sweep_cap_reached":    "Reconciled sweep cap reached",
    "daily_alpha_no_admits": "No trades approved today",
    "gap_detected":         "Monitoring gap detected",
    # Orchestrator-prefixed gaps (orch_gap:<layer>).
    "orch_gap:daily_alpha":        "No daily-alpha picks admitted",
    "orch_gap:daily_alpha_executor": "Daily-alpha executor failed",
    "orch_gap:loop_novelty":       "Loop novelty flagged",
    "orch_gap:decision_truth":     "Decision audit flagged",
    "orch_gap:research_truth":     "Research audit flagged",
    "orch_gap:card_truth":         "Card audit flagged",
    "orch_gap:reconciled_sweeper": "Reconciled sweep pending",
    "orch_gap:tp_agent":           "Take-profit agent failed",
    "orch_gap:daily_audit":        "Daily system audit flagged",
}


def translate(key: str) -> str:
    """Translate a single technical label into operator English.

    Rules (in order):
      1. Exact match in _LABELS → return mapped string.
      2. "<prefix>:<value>" form: try prefix first, then "<prefix>:" + any.
         e.g. "orch_gap:daily_alpha" matches the table; a future
         "orch_gap:something_new" falls through to humanize logic.
      3. Dotted form "a.b.c" → humanize last segment + prefix parent.
      4. Snake_case → "Snake case" (capitalize first, spaces instead of _).
    """
    if not key:
        return ""
    if key in _LABELS:
        return _LABELS[key]
    # Colon form (e.g. orch_gap:something_else).
    if ":" in key:
        prefix, _, suffix = key.partition(":")
        # Try prefix alone.
        prefix_label = _LABELS.get(prefix) or _humanize(prefix)
        suffix_label = _LABELS.get(suffix) or _humanize(suffix)
        return f"{prefix_label} — {suffix_label}"
    # Dotted form (e.g. scheduler.fail → already mapped, but "some.new.event"
    # should still humanize).
    if "." in key:
        parts = key.split(".")
        return " — ".join(_humanize(p) for p in parts)
    # Bare snake_case.
    return _humanize(key)


def _humanize(token: str) -> str:
    """'some_tech_event' → 'Some tech event'. 'UPPER_CONST' → 'Upper const'."""
    if not token:
        return ""
    cleaned = token.replace("_", " ").replace("-", " ").strip()
    if not cleaned:
        return ""
    return cleaned[:1].upper() + cleaned[1:].lower()


def translate_alert(alert: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the alert dict with a `label` field populated
    from `kind`. Non-destructive; leaves original fields intact."""
    out = dict(alert) if alert else {}
    kind = out.get("kind", "")
    out["label"] = translate(kind)
    return out


def translate_batch(keys: list[str]) -> list[dict[str, str]]:
    """Batch helper for endpoint return shape."""
    return [{"key": k, "label": translate(k)} for k in keys or []]
