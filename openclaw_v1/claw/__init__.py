"""
Claw — audit, monitoring, execution-tracking layer (CLAW-NIC-v1).

Non-Interference Contract (CLAW-NIC-v1):
    Claw may NEVER modify bot scores, confidence, rationale, or trade plan.
    Claw may only: ingest unchanged, store immutably, track execution,
    monitor infrastructure, reconcile state, show truth, log incidents,
    provide commentary (tagged non-authoritative), and self-heal infra.

The trading bot remains the sole brain, sole scoring engine, sole planner.
"""

from .contract import (
    CLAW_NIC_VERSION,
    BOT_FIELDS_FROZEN,
    BotPayloadMutation,
    bot_payload_hash,
    freeze,
    assert_unchanged,
    canonical_payload,
)
from .ingest import record_bot_decision, fetch_bot_decision, list_bot_decisions
from .db import init_claw_schema, claw_db_path
from . import execution_tracker
from . import reconciler
from . import watchdog
from . import commentary
from . import notify
from . import llm
# ai_augment removed — replaced by APEX-Ω 5-LLM consensus in /llm/consensus.py
from .incidents import open_incident, resolve_incident, list_incidents

__all__ = [
    "CLAW_NIC_VERSION",
    "BOT_FIELDS_FROZEN",
    "BotPayloadMutation",
    "bot_payload_hash",
    "freeze",
    "assert_unchanged",
    "canonical_payload",
    "record_bot_decision",
    "fetch_bot_decision",
    "list_bot_decisions",
    "init_claw_schema",
    "claw_db_path",
    "execution_tracker",
    "reconciler",
    "watchdog",
    "commentary",
    "notify",
    "llm",
    "open_incident",
    "resolve_incident",
    "list_incidents",
]
