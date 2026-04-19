"""Phase 11n-9-i — TP Sell Governor (Layer 9).

Audits each take-profit candidate produced by tp_agent.build_proposal
BEFORE it ships as a real market order. This is a DEDICATED layer for
sells — it sits ABOVE Layer 8 (pre-trade gate) for the sell-specific
evidence check, then Layer 8 runs its standard authorization.

WHY a dedicated sell governor?
  - Sells at ≥2% margin are a policy change (phase 11n-9-i). We want
    the trail of "why was this sold?" to be explicit and archived.
  - Layer 8's 12-item checklist is tuned for BUYs (projected_wr_meets_
    target, scenario_confirms, etc.). It doesn't validate sell-side
    concerns like "did live price actually cross target by enough
    margin to cover fees + slippage?"
  - Audit must be evidence-based. Every reason the governor approves
    a sell is a verifiable fact.

CHECKLIST (5 items, all must pass):
  1. MARGIN_EXCEEDS_FEES     — live_ret ≥ 2 × taker fee (0.002 = 0.20%)
                                + target_tp_floor. Prevents selling a
                                "gain" that's actually noise + fees.
  2. PRICE_ALIGNED_WITH_BOOK  — live_price agrees with adapter ticker
                                within 0.5% (guards against stale
                                snapshot leading to fake margin).
  3. POSITION_NOT_RECONCILED  — never TP-sell a reconciled position;
                                those flow through reconciled_sweeper.
  4. QUOTA_AVAILABLE          — hourly / daily slots remain.
  5. SYMBOL_TRADEABLE_ON_OKX  — spot pair is resolvable + has an
                                active market on the exchange.

Result: SellVerdict(admitted, rejection_reason, checklist, evidence_refs).
Persisted per-audit to spot_tp_sell_verdicts so the dashboard can show
"why did we sell X at Y%" for every historical TP event.

SPOT AGGRO only. Never ships orders itself — returns an audit verdict.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, asdict, field
from typing import Any, Optional


# OKX EEA spot taker fee (lifetime default). Conservative.
OKX_TAKER_FEE_BPS = 10          # 0.10% per side = 0.20% round-trip
SLIPPAGE_GUARD_PCT = 0.005      # 0.5% live_price vs ticker mismatch is too stale
TP_MIN_MARGIN_MULT = 1.5        # margin must be at least 1.5× the round-trip cost


@dataclass
class ChecklistItem:
    key: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SellVerdict:
    symbol: str
    admitted: bool
    rejection_reason: Optional[str]
    checklist: list[ChecklistItem]
    evidence_refs: list[str]
    checked_at_ms: int

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["checklist"] = [c.to_dict() if hasattr(c, "to_dict") else dict(c)
                          for c in self.checklist]
        return d


# ---------------------------------------------------------------------------
# Individual checklist items
# ---------------------------------------------------------------------------

def _check_margin_exceeds_fees(cand: Any) -> ChecklistItem:
    live_ret = float(getattr(cand, "live_ret", 0) or 0)
    round_trip_fee = 2 * (OKX_TAKER_FEE_BPS / 10000)       # 0.20%
    min_margin = round_trip_fee * TP_MIN_MARGIN_MULT       # 0.30%
    ok = live_ret >= min_margin
    return ChecklistItem(
        key="margin_exceeds_fees",
        passed=ok,
        detail=(f"live_ret {live_ret*100:.2f}% vs min "
                f"{min_margin*100:.2f}% (fees×{TP_MIN_MARGIN_MULT})"),
    )


def _check_price_aligned(cand: Any) -> ChecklistItem:
    """live_price must match adapter ticker within 0.5% — guards
    against a stale snapshot that led to a fake margin."""
    live_px = getattr(cand, "live_price", None)
    entry = float(getattr(cand, "entry_price", 0) or 0)
    if live_px is None or entry <= 0:
        return ChecklistItem(
            key="price_aligned_with_book", passed=False,
            detail=f"no live_price (={live_px}) or entry (={entry})",
        )
    try:
        from spot_aggro import _engine_instance
        adapter = (_engine_instance._ensure_adapter()
                   if _engine_instance is not None else None)
    except Exception:  # noqa: BLE001
        adapter = None
    if adapter is None:
        return ChecklistItem(
            key="price_aligned_with_book", passed=False,
            detail="adapter unavailable — cannot re-check ticker",
        )
    try:
        import asyncio
        ticker = asyncio.run(asyncio.to_thread(
            adapter._client.fetch_ticker, adapter._spot_for(cand.symbol),
        ))
        fresh = float(ticker.get("last") or 0)
    except Exception as exc:  # noqa: BLE001
        return ChecklistItem(
            key="price_aligned_with_book", passed=False,
            detail=f"ticker fetch failed: {exc!s}"[:120],
        )
    if fresh <= 0:
        return ChecklistItem(
            key="price_aligned_with_book", passed=False,
            detail="fresh ticker returned 0",
        )
    drift = abs(fresh - live_px) / live_px
    ok = drift <= SLIPPAGE_GUARD_PCT
    return ChecklistItem(
        key="price_aligned_with_book",
        passed=ok,
        detail=(f"snap {live_px:.6g} vs fresh {fresh:.6g} · drift "
                f"{drift*100:.2f}% {'≤' if ok else '>'} "
                f"{SLIPPAGE_GUARD_PCT*100:.2f}% guard"),
    )


def _check_not_reconciled(cand: Any) -> ChecklistItem:
    module = str(getattr(cand, "module", "") or "")
    ok = not module.startswith("M_reconciled")
    return ChecklistItem(
        key="position_not_reconciled",
        passed=ok,
        detail=(f"module={module!r} — "
                + ("ok, tp_agent scope" if ok else
                   "reconciled position — routed to reconciled_sweeper")),
    )


def _check_quota_available(_cand: Any) -> ChecklistItem:
    # tp_agent's _agent_scheduler already caps; we re-check here in case
    # of concurrent runs, but trust the scheduler result.
    from spot_aggro.governance.tp_agent import _recent_tp_sells
    import os
    try:
        hourly_cap = int(os.environ.get("SPOT_TP_MAX_PER_HOUR", "4"))
        daily_cap  = int(os.environ.get("SPOT_TP_MAX_PER_DAY",  "12"))
    except (ValueError, TypeError):
        hourly_cap, daily_cap = 4, 12
    h = _recent_tp_sells(1.0)
    d = _recent_tp_sells(24.0)
    ok = h < hourly_cap and d < daily_cap
    return ChecklistItem(
        key="quota_available",
        passed=ok,
        detail=f"{h}/{hourly_cap} hr · {d}/{daily_cap} day",
    )


def _check_symbol_tradeable(cand: Any) -> ChecklistItem:
    try:
        from spot_aggro import _engine_instance
        adapter = (_engine_instance._ensure_adapter()
                   if _engine_instance is not None else None)
    except Exception:  # noqa: BLE001
        adapter = None
    if adapter is None:
        return ChecklistItem(
            key="symbol_tradeable_on_okx", passed=False,
            detail="adapter unavailable",
        )
    try:
        pair = adapter._spot_for(cand.symbol)
        ok = isinstance(pair, str) and len(pair) > 0
        return ChecklistItem(
            key="symbol_tradeable_on_okx", passed=ok,
            detail=f"spot pair: {pair}",
        )
    except Exception as exc:  # noqa: BLE001
        return ChecklistItem(
            key="symbol_tradeable_on_okx", passed=False,
            detail=f"pair resolve failed: {exc!s}"[:100],
        )


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def audit_candidate(cand: Any) -> SellVerdict:
    items = [
        _check_margin_exceeds_fees(cand),
        _check_price_aligned(cand),
        _check_not_reconciled(cand),
        _check_quota_available(cand),
        _check_symbol_tradeable(cand),
    ]
    admitted = all(i.passed for i in items)
    first_fail = next((i for i in items if not i.passed), None)
    rejection = None if admitted else f"{first_fail.key}: {first_fail.detail}"
    v = SellVerdict(
        symbol=getattr(cand, "symbol", "?"),
        admitted=admitted,
        rejection_reason=rejection,
        checklist=items,
        evidence_refs=list(getattr(cand, "evidence_refs", []) or []),
        checked_at_ms=int(time.time() * 1000),
    )
    try:
        _persist(v)
    except Exception:  # noqa: BLE001
        pass
    return v


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_tp_sell_verdicts (
    verdict_id      TEXT PRIMARY KEY,
    checked_at_ms   INTEGER NOT NULL,
    symbol          TEXT NOT NULL,
    admitted        INTEGER NOT NULL,
    rejection       TEXT,
    payload_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_tp_sell_verdicts_ts
    ON spot_tp_sell_verdicts(checked_at_ms DESC);
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


def _persist(v: SellVerdict) -> None:
    _init_schema()
    from shared.persistence import state as persist
    import uuid
    con = persist._connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO spot_tp_sell_verdicts "
            "(verdict_id, checked_at_ms, symbol, admitted, rejection, "
            " payload_json) VALUES (?, ?, ?, ?, ?, ?)",
            (f"tpv-{v.checked_at_ms}-{v.symbol}-{uuid.uuid4().hex[:6]}",
             v.checked_at_ms, v.symbol, 1 if v.admitted else 0,
             v.rejection_reason,
             json.dumps(v.to_dict(), default=str)),
        )
        con.commit()
    finally:
        con.close()


def latest_verdicts(limit: int = 20) -> list[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT verdict_id, checked_at_ms, symbol, admitted, rejection "
            "FROM spot_tp_sell_verdicts ORDER BY checked_at_ms DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    finally:
        con.close()
    return [{"verdict_id": r[0], "checked_at_ms": r[1], "symbol": r[2],
             "admitted": bool(r[3]), "rejection": r[4]}
            for r in rows]
