"""Phase 11n-9-i — Take-Profit Agent Team.

POLICY CHANGE (operator decision 2026-04-19): any open spot position
with live unrealized return ≥ TP_TARGET (default 0.02 = 2%) becomes a
take-profit CANDIDATE. Instead of shipping the first one we see, a
team of four agents ranks the candidates and proposes an ordered sell
list. The proposed list is then audited by the Layer-9 TP Sell
Governor (tp_sell_gov.py) — which validates evidence-based grounds
for each sell — before any order actually ships.

AGENT TEAM (4 roles):
  1. SCOUT      — discovers candidates (live_ret ≥ TP_TARGET)
  2. RANKER     — ranks by margin × symbol-quality × time-decay
  3. SCHEDULER  — caps per-hour / per-day + spaces sells to limit slippage
  4. REVIEWER   — cross-checks the ranked list against open book:
                  reconciled positions go to the bottom (safety),
                  scored positions with strong scenario hypotheses
                  may get held even at 2% if projection says 5%+.

OUTPUT: TPProposal with an ordered list[TPCandidate]. Every candidate
carries a rationale + evidence_refs so the governor can audit.

SAFETY:
  - Never runs on reconciled positions UNLESS they cross a big-win
    threshold (handled by reconciled_sweeper at ≥15%).
  - Margin floor is an env-configurable hard minimum; nothing below
    it is ever shipped, governor or no governor.
  - Dry-run by default; SPOT_TP_EXECUTE=1 to actually ship (default
    "1" in phase-11n-9-i-e since auto flags are on).

SPOT AGGRO only. Read-only toward engine state except the final sell
which goes through pre_trade_gov (Layer 8) + tp_sell_gov (Layer 9).
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, asdict, field
from typing import Any, Optional


log = logging.getLogger(__name__)

# Thresholds — tunable via env.
DEFAULT_TP_TARGET = 0.02        # ≥2% live return = candidate
HARD_TP_FLOOR = 0.015           # never ship below 1.5% no matter what
MAX_SELLS_PER_HOUR = 4
MAX_SELLS_PER_DAY = 12


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (ValueError, TypeError):
        return default


def tp_target() -> float:
    return max(HARD_TP_FLOOR,
               _env_float("SPOT_TP_TARGET", DEFAULT_TP_TARGET))


def is_execute_enabled() -> bool:
    """Opt-out via TRADE_DRY_RUN / SPOT_DRY_RUN / SPOT_TP_EXECUTE=0."""
    if os.environ.get("TRADE_DRY_RUN", "0").strip() == "1":
        return False
    if os.environ.get("SPOT_DRY_RUN", "0").strip() == "1":
        return False
    return os.environ.get("SPOT_TP_EXECUTE", "1").strip() == "1"


@dataclass
class TPCandidate:
    symbol: str
    tier: str
    module: str
    value_usd: float
    entry_price: float
    live_price: Optional[float]
    live_ret: float
    margin_usd: float               # value_usd * live_ret
    age_h: float
    rank_score: float               # composite rank — higher = sell first
    rationale: str                  # one-line plain-English reason
    evidence_refs: list[str] = field(default_factory=list)
    # Filled by governor after audit:
    admitted: bool = False          # True iff Layer 9 approves
    rejection_reason: Optional[str] = None
    executed: bool = False          # True iff sell shipped
    authz_id: Optional[str] = None  # pre_trade_gov authz id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TPProposal:
    proposal_id: str
    generated_ts_ms: int
    tp_target: float
    candidates: list[TPCandidate]
    scheduler_cap_remaining: dict[str, int]   # {"hourly": N, "daily": M}
    summary: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["candidates"] = [c.to_dict() if hasattr(c, "to_dict") else dict(c)
                           for c in self.candidates]
        return d


# ---------------------------------------------------------------------------
# AGENT 1 — SCOUT. Discovers candidates from engine state + live prices.
# ---------------------------------------------------------------------------

def _agent_scout(target: float) -> list[TPCandidate]:
    try:
        from spot_aggro import _engine_instance
    except Exception:  # noqa: BLE001
        return []
    if _engine_instance is None:
        return []
    positions = dict(getattr(_engine_instance.state, "positions", {}))
    if not positions:
        return []
    try:
        live = _engine_instance._snapshot_live_prices(list(positions.keys()))
    except Exception:  # noqa: BLE001
        live = {}
    out: list[TPCandidate] = []
    for sym, pos in positions.items():
        module = getattr(pos, "module", "") or ""
        # Skip reconciled (handled by reconciled_sweeper).
        if module.startswith("M_reconciled"):
            continue
        entry = float(getattr(pos, "entry_price", 0) or 0)
        px = live.get(sym)
        if px is None or entry <= 0:
            continue
        ret = (px - entry) / entry
        if ret < target:
            continue
        value_usd = float(getattr(pos, "size_usd", 0) or 0)
        entry_time = float(getattr(pos, "entry_time", 0) or 0)
        age_h = max(0.0, (time.time() - entry_time) / 3600.0) if entry_time > 0 else 0.0
        out.append(TPCandidate(
            symbol=sym,
            tier=getattr(pos, "tier", "?") or "?",
            module=module or "unknown",
            value_usd=value_usd,
            entry_price=entry,
            live_price=px,
            live_ret=ret,
            margin_usd=value_usd * ret,
            age_h=age_h,
            rank_score=0.0,      # filled by ranker
            rationale="",
            evidence_refs=[f"position://{sym}",
                           f"live_price://{sym}={px}"],
        ))
    return out


# ---------------------------------------------------------------------------
# AGENT 2 — RANKER. Scores each candidate and sorts.
# ---------------------------------------------------------------------------

def _agent_ranker(cands: list[TPCandidate]) -> list[TPCandidate]:
    """Rank score = (margin_usd × 10) × quality_mult × time_decay.
    Higher = sell first.
       - margin_usd: absolute $ to lock in.
       - quality_mult: pulled from research.per_symbol WR (0.7 low → 1.3 high).
       - time_decay: 1.0 early, 1.2 after 8h (don't let paper profit linger).
    """
    try:
        from spot_aggro.governance.research_agent import latest_report
        research = latest_report() or {}
    except Exception:  # noqa: BLE001
        research = {}
    per_sym = research.get("per_symbol") or []
    sym_wr: dict[str, float] = {}
    for r in per_sym:
        s = r.get("symbol")
        wr = r.get("win_rate")
        n = int(r.get("exits") or 0)
        if s and wr is not None and n >= 3:
            # Keep the best WR row per symbol (there may be one row per
            # (symbol, tier) pair — take max).
            sym_wr[s] = max(sym_wr.get(s, 0.0), float(wr))

    for c in cands:
        wr = sym_wr.get(c.symbol)
        if wr is None:
            qual = 1.0
        elif wr >= 0.70:
            qual = 1.30
        elif wr >= 0.55:
            qual = 1.10
        elif wr >= 0.40:
            qual = 1.00
        else:
            qual = 0.80
        decay = 1.20 if c.age_h >= 8 else (1.10 if c.age_h >= 4 else 1.00)
        c.rank_score = round(abs(c.margin_usd) * 10.0 * qual * decay, 4)
        c.rationale = (f"margin ${c.margin_usd:.2f} · {c.live_ret*100:.2f}% · "
                       f"qual×{qual:.2f} · decay×{decay:.2f}")
        c.evidence_refs.append(f"research://{research.get('report_id','?')}")
    cands.sort(key=lambda x: -x.rank_score)
    return cands


# ---------------------------------------------------------------------------
# AGENT 3 — SCHEDULER. Caps per-hour / per-day.
# ---------------------------------------------------------------------------

def _recent_tp_sells(hours: float) -> int:
    _init_schema()
    from shared.persistence import state as persist
    cutoff_ms = int((time.time() - hours * 3600) * 1000)
    con = persist._connect()
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM spot_tp_executions "
            "WHERE ts_ms > ? AND placed = 1",
            (cutoff_ms,),
        ).fetchone()
    finally:
        con.close()
    return int(row[0]) if row else 0


def _agent_scheduler(cands: list[TPCandidate]) -> tuple[list[TPCandidate], dict[str, int]]:
    hourly_cap = _env_int("SPOT_TP_MAX_PER_HOUR", MAX_SELLS_PER_HOUR)
    daily_cap  = _env_int("SPOT_TP_MAX_PER_DAY",  MAX_SELLS_PER_DAY)
    used_1h = _recent_tp_sells(1.0)
    used_24h = _recent_tp_sells(24.0)
    slots_hour = max(0, hourly_cap - used_1h)
    slots_day  = max(0, daily_cap - used_24h)
    slots = min(slots_hour, slots_day)
    # Mark everything beyond the slot count as "waiting next window".
    for i, c in enumerate(cands):
        if i >= slots:
            c.rejection_reason = (
                f"daily/hourly cap reached "
                f"({used_24h}/{daily_cap} day · {used_1h}/{hourly_cap} hr)"
            )
    return cands, {"hourly_remaining": slots_hour,
                   "daily_remaining": slots_day,
                   "slots_this_run": slots}


# ---------------------------------------------------------------------------
# AGENT 4 — REVIEWER. Holds position when scenario says bigger move coming.
# ---------------------------------------------------------------------------

def _agent_reviewer(cands: list[TPCandidate]) -> list[TPCandidate]:
    try:
        from spot_aggro.governance.scenario_runner import latest_batch_for_tier
    except Exception:  # noqa: BLE001
        latest_batch_for_tier = None
    for c in cands:
        if c.rejection_reason:
            continue
        if c.live_ret >= 0.05:
            # Already ≥5%, don't second-guess.
            continue
        if latest_batch_for_tier is None:
            continue
        try:
            batch = latest_batch_for_tier(c.tier) or {}
        except Exception:  # noqa: BLE001
            batch = {}
        best = (batch.get("best_outcome") or {}) if isinstance(batch, dict) else {}
        scen_wr = best.get("simulated_wr")
        # If scenario suggests tier can hit ≥70% WR AND position is young,
        # recommend holding. Evidence-based override.
        if (scen_wr is not None and scen_wr >= 0.70
                and c.age_h < 2.0 and c.live_ret < 0.035):
            c.rejection_reason = (
                f"reviewer HOLD: scenario proj WR {scen_wr*100:.0f}% · "
                f"age {c.age_h:.1f}h · potential >5% move"
            )
            c.evidence_refs.append(
                f"scenario://{batch.get('batch_id','none') if batch else 'none'}"
            )
    return cands


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def build_proposal() -> TPProposal:
    target = tp_target()
    ts_ms = int(time.time() * 1000)
    proposal_id = f"tp-{ts_ms}-{uuid.uuid4().hex[:6]}"
    cands = _agent_scout(target)
    cands = _agent_ranker(cands)
    cands, caps = _agent_scheduler(cands)
    cands = _agent_reviewer(cands)
    admitted = [c for c in cands if not c.rejection_reason]
    rejected = [c for c in cands if c.rejection_reason]
    total_margin = sum(c.margin_usd for c in admitted)
    summary = (f"{len(cands)} TP candidates ≥ {target*100:.2f}% · "
               f"{len(admitted)} admitted by agent team "
               f"(${total_margin:.2f} margin) · "
               f"{len(rejected)} held/capped · "
               f"caps {caps.get('slots_this_run', 0)} slots this run")
    return TPProposal(
        proposal_id=proposal_id,
        generated_ts_ms=ts_ms,
        tp_target=target,
        candidates=cands,
        scheduler_cap_remaining=caps,
        summary=summary,
    )


def execute_proposal(proposal: TPProposal) -> TPProposal:
    """Ship each admitted candidate through Layer 9 audit → Layer 8 gate →
    OKX adapter. Updates candidate.executed flag."""
    if not is_execute_enabled():
        return proposal

    from spot_aggro.governance import tp_sell_gov
    from spot_aggro.governance import pre_trade_gov

    import asyncio

    try:
        from spot_aggro import _engine_instance
    except Exception:  # noqa: BLE001
        _engine_instance = None
    if _engine_instance is None:
        return proposal

    for c in proposal.candidates:
        if c.rejection_reason:
            continue
        # Layer 9 evidence audit.
        verdict = tp_sell_gov.audit_candidate(c)
        c.admitted = verdict.admitted
        if not verdict.admitted:
            c.rejection_reason = f"layer9: {verdict.rejection_reason}"
            continue
        # Layer 8 pre-trade gate (uses tp_sell source, bypasses heavy
        # checklist because we already did evidence audit in Layer 9).
        authz = pre_trade_gov.authorize_trade(
            c.symbol, "sell", c.tier, source="tp_sell",
        )
        c.authz_id = authz.authz_id
        if not authz.passed:
            c.rejection_reason = f"layer8: {authz.rejection_reason}"
            continue
        # Ship it.
        try:
            pos = _engine_instance.state.positions.get(c.symbol)
            if pos is None:
                c.rejection_reason = "position gone"
                continue
            qty = pos.size_usd / max(pos.entry_price, 1e-9)
            adapter = _engine_instance._ensure_adapter()
            asyncio.run(asyncio.to_thread(
                adapter._client.create_market_order,
                adapter._spot_for(c.symbol), "sell", qty,
                {"tdMode": "cash"},
            ))
            _engine_instance.state.positions.pop(c.symbol, None)
            c.executed = True
            _record_execution(c, placed=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("TP sell failed for %s: %s", c.symbol, exc)
            c.rejection_reason = f"adapter: {exc!s}"[:120]
            _record_execution(c, placed=False)
    return proposal


def build_and_execute() -> TPProposal:
    p = build_proposal()
    p = execute_proposal(p)
    try:
        _persist(p)
    except Exception:  # noqa: BLE001
        pass
    return p


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_tp_proposals (
    proposal_id     TEXT PRIMARY KEY,
    generated_ts_ms INTEGER NOT NULL,
    tp_target       REAL NOT NULL,
    n_admitted      INTEGER NOT NULL,
    n_rejected      INTEGER NOT NULL,
    total_margin    REAL NOT NULL,
    payload_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_tp_proposals_ts
    ON spot_tp_proposals(generated_ts_ms DESC);

CREATE TABLE IF NOT EXISTS spot_tp_executions (
    execution_id    TEXT PRIMARY KEY,
    ts_ms           INTEGER NOT NULL,
    symbol          TEXT NOT NULL,
    tier            TEXT NOT NULL,
    live_ret        REAL NOT NULL,
    margin_usd      REAL NOT NULL,
    placed          INTEGER NOT NULL,
    rationale       TEXT,
    payload_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_tp_exec_ts
    ON spot_tp_executions(ts_ms DESC);
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


def _persist(p: TPProposal) -> None:
    _init_schema()
    from shared.persistence import state as persist
    admitted = [c for c in p.candidates if not c.rejection_reason]
    rejected = [c for c in p.candidates if c.rejection_reason]
    total = sum(c.margin_usd for c in admitted)
    con = persist._connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO spot_tp_proposals "
            "(proposal_id, generated_ts_ms, tp_target, n_admitted, "
            " n_rejected, total_margin, payload_json) VALUES "
            "(?, ?, ?, ?, ?, ?, ?)",
            (p.proposal_id, p.generated_ts_ms, p.tp_target,
             len(admitted), len(rejected), total,
             json.dumps(p.to_dict(), default=str)),
        )
        con.commit()
    finally:
        con.close()


def _record_execution(c: TPCandidate, *, placed: bool) -> None:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO spot_tp_executions "
            "(execution_id, ts_ms, symbol, tier, live_ret, margin_usd, "
            " placed, rationale, payload_json) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (c.authz_id or f"tpx-{int(time.time()*1000)}-{c.symbol}",
             int(time.time() * 1000), c.symbol, c.tier,
             c.live_ret, c.margin_usd, 1 if placed else 0,
             c.rationale, json.dumps(c.to_dict(), default=str)),
        )
        con.commit()
    finally:
        con.close()


def latest_proposal() -> Optional[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        row = con.execute(
            "SELECT payload_json FROM spot_tp_proposals "
            "ORDER BY generated_ts_ms DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    return json.loads(row[0]) if row else None


def executions_today() -> list[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    cutoff_ms = int((time.time() - 86400) * 1000)
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT execution_id, ts_ms, symbol, tier, live_ret, margin_usd, "
            " placed, rationale FROM spot_tp_executions "
            "WHERE ts_ms > ? ORDER BY ts_ms DESC",
            (cutoff_ms,),
        ).fetchall()
    finally:
        con.close()
    return [
        {"execution_id": r[0], "ts_ms": r[1], "symbol": r[2], "tier": r[3],
         "live_ret": r[4], "margin_usd": r[5],
         "placed": bool(r[6]), "rationale": r[7]}
        for r in rows
    ]
