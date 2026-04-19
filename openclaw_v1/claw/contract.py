"""
CLAW-NIC-v1 — Non-Interference Contract.

Bot outputs are frozen the moment they cross into Claw. Claw may read them,
record them, reference them, but never modify them. This module provides the
primitives that *enforce* that:

    freeze(payload)           -> read-only MappingProxyType view (writes raise)
    canonical_payload(p)      -> stable dict restricted to frozen keys
    bot_payload_hash(p)       -> sha256 over canonical payload (stable across runs)
    assert_unchanged(a, b)    -> raises BotPayloadMutation if frozen keys diverge

The frozen key tuple BOT_FIELDS_FROZEN is the single source of truth for
"what belongs to the bot". Extend with care — every addition is a new
commitment that Claw must never mutate.
"""

from __future__ import annotations

import hashlib
import json
from types import MappingProxyType
from typing import Any, Iterable, Mapping


CLAW_NIC_VERSION = "CLAW-NIC-v1"


# The authoritative list of fields Claw must never modify. Any bot output
# that includes one of these keys commits Claw to preserving it byte-for-byte.
BOT_FIELDS_FROZEN: tuple[str, ...] = (
    # Scoring & confidence
    "PWinPct",
    "ConfidencePct",
    "ScoreTotal",
    "EV_R",
    "RRTrue",
    # Terminal routing
    "TerminalAction",
    "FinalVerdict",
    "action",
    "decision",
    # Trade plan
    "entry_px",
    "stop_px",
    "target_px",
    "EntryPx",
    "StopPx",
    "TargetPx",
    # Provenance
    "rationale",
    "model_version",
    "strategy_id",
    "cycle_id",
    "cycleId",
    "symbol",
    # Binary / Kelly specifics
    "edge",
    "final_f",
    "kelly_f",
    "p",
    "c",
)


class BotPayloadMutation(Exception):
    """Raised when Claw detects a frozen bot field has been modified."""


# ---------------------------------------------------------------------------
# Freezing
# ---------------------------------------------------------------------------

def freeze(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a read-only view of *payload*.

    Nested dicts are frozen recursively. Lists/tuples are converted to tuples
    (read-only). Other values pass through unchanged.

    Attempting to mutate the returned mapping raises TypeError at the Python
    level — the payload is immutable from the caller's perspective.
    """
    if payload is None:
        return MappingProxyType({})
    if not isinstance(payload, Mapping):
        raise TypeError(f"freeze() requires a mapping, got {type(payload).__name__}")
    out: dict[str, Any] = {}
    for k, v in payload.items():
        out[str(k)] = _deep_freeze(v)
    return MappingProxyType(out)


def _deep_freeze(v: Any) -> Any:
    if isinstance(v, Mapping):
        return freeze(v)
    if isinstance(v, (list, tuple)):
        return tuple(_deep_freeze(x) for x in v)
    return v


# ---------------------------------------------------------------------------
# Canonicalisation + hashing
# ---------------------------------------------------------------------------

def canonical_payload(
    payload: Mapping[str, Any],
    *,
    fields: Iterable[str] = BOT_FIELDS_FROZEN,
) -> dict[str, Any]:
    """Return a dict restricted to frozen keys that are actually present.

    The subset is deterministic and stable across runs — keys sorted, values
    canonicalised (nested dicts sorted, tuples->lists for JSON-compat).
    """
    subset: dict[str, Any] = {}
    seen_keys = set(fields)
    for k, v in payload.items():
        if k in seen_keys:
            subset[k] = _to_jsonable(v)
    return dict(sorted(subset.items()))


def _to_jsonable(v: Any) -> Any:
    if isinstance(v, Mapping):
        return {k: _to_jsonable(x) for k, x in sorted(v.items())}
    if isinstance(v, (list, tuple)):
        return [_to_jsonable(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    # Fallback — stringify unknown types so hashing stays stable.
    return str(v)


def bot_payload_hash(payload: Mapping[str, Any]) -> str:
    """SHA-256 over the canonicalised frozen subset of *payload*."""
    canon = canonical_payload(payload)
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# Contract enforcement
# ---------------------------------------------------------------------------

def assert_unchanged(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    where: str = "<unknown>",
) -> None:
    """Raise BotPayloadMutation if any frozen key diverges between *before* and *after*.

    Use this on both sides of any operation that handles a bot payload:

        before = freeze(payload)
        ...operation...
        assert_unchanged(before, payload, where="gateway.execute_intent")
    """
    before_canon = canonical_payload(before)
    after_canon = canonical_payload(after)
    if before_canon != after_canon:
        diffs = []
        keys = set(before_canon) | set(after_canon)
        for k in sorted(keys):
            b = before_canon.get(k, "<missing>")
            a = after_canon.get(k, "<missing>")
            if b != a:
                diffs.append(f"{k}: {b!r} -> {a!r}")
        raise BotPayloadMutation(
            f"{CLAW_NIC_VERSION} violation at {where}: " + "; ".join(diffs)
        )
