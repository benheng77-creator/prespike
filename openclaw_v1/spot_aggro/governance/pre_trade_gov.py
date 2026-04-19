"""Phase 11n-9-b — Pre-Trade Governor (Layer 8, universal gate).

Every order the engine is about to place is FIRST authorized through
this gate. It reuses the Layer 8 checklist logic from
daily_alpha_gov so the same bar applies to:
  - Daily Alpha picks (when the execution layer eventually sends them)
  - Normal SPOT AGGRO module entries (M1_flow, M1_squeeze, M1_scalp,
    M3_blitz)
  - Any operator-triggered buy/sell routed through the adapter

Contract:
  authorize_trade(symbol, side, tier, source) -> TradeAuthorization

TradeAuthorization.passed is True iff every applicable checklist item
passes. .passed=False must block execution at the adapter boundary.
The full checklist (with per-item pass/fail + detail) is attached so
the dashboard can show exactly which item blocked.

Every authorization decision is persisted to
spot_pre_trade_authorizations so the dashboard can display the last
N decisions and the operator can audit what the governor allowed vs
what it blocked.

Read-only wrt. OKX. Writes only to its own table.

SPOT AGGRO only.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, asdict, field
from typing import Any, Optional


DEFAULT_TARGET_PROJ_WR = 0.70


@dataclass
class TradeAuthorization:
    authz_id: str
    ts_ms: int
    symbol: str
    side: str                      # "buy" | "sell"
    tier: str
    source: str                    # "daily_alpha" | "engine_entry" | "engine_exit" | "manual"
    passed: bool
    score: float                   # 0..1
    checklist: list[dict[str, Any]]
    rejection_reason: Optional[str]
    target_proj_wr: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _synth_pick(symbol: str, side: str, tier: str) -> Any:
    """Build a minimal pick-like object so we can reuse the 12-item
    checklist from daily_alpha_gov. We pull the real per-symbol stats
    from the latest research report."""
    from spot_aggro.governance.research_agent import latest_report
    research = latest_report() or {}
    # Phase 11n-9-g: per_symbol rows are keyed (symbol, tier) because a
    # coin can trade under multiple tiers. The pre-trade gate's
    # sample_size check should see ALL closed exits for the symbol,
    # regardless of which tier historically traded it. Aggregate
    # matching rows before running the check.
    all_rows = [r for r in (research.get("per_symbol") or [])
                if r.get("symbol") == symbol]
    if all_rows:
        row = {
            "symbol": symbol, "tier": tier,
            "exits":  sum(int(r.get("exits", 0)  or 0) for r in all_rows),
            "wins":   sum(int(r.get("wins", 0)   or 0) for r in all_rows),
            "losses": sum(int(r.get("losses", 0) or 0) for r in all_rows),
            "pnl_usd": sum(float(r.get("pnl_usd", 0) or 0) for r in all_rows),
        }
        row["win_rate"] = (row["wins"] / row["exits"]
                          if row["exits"] else None)
    else:
        row = {"symbol": symbol, "tier": tier, "wins": 0, "losses": 0,
               "win_rate": None, "exits": 0, "pnl_usd": 0.0}
    tier_row = next(
        (s for s in (research.get("tier_stats") or [])
         if s.get("tier") == tier),
        {},
    )
    # Duck-typed object matching AlphaPick's attributes.
    from types import SimpleNamespace
    tier_wr = tier_row.get("primary_wr")
    sym_wr = row.get("win_rate")
    # Blend for projected_wr identical to daily_alpha semantics.
    weights = [(sym_wr, 0.60), (tier_wr, 0.25)]
    tw = sum(w for v, w in weights if v is not None)
    proj_wr = (sum((v or 0) * w for v, w in weights if v is not None) / tw
               if tw > 0 else None)
    factors = [
        {"name": "projected_wr", "value": proj_wr, "weight": 0.3,
         "why": f"blended proj WR = {(proj_wr or 0)*100:.1f}%",
         "severity": "positive" if (proj_wr or 0) >= DEFAULT_TARGET_PROJ_WR
                     else "warn"},
    ]
    pick = SimpleNamespace(
        symbol=symbol, tier=tier, action=side,
        projected_wr=proj_wr,
        sample_size=int(row.get("exits") or 0),
        confidence=min(0.99, max(0.0, proj_wr or 0.0)),
        rationale=f"pre-trade authorization for {side} {symbol}",
        factors=factors,
        evidence_refs=[
            f"research://{research.get('report_id','?')}",
            f"per_symbol://{symbol}",
        ],
        checklist_pass=False,
        checklist_score=0.0,
    )
    return pick, research


def _toggle_snapshot() -> dict[str, bool]:
    try:
        from spot_aggro.api import routes as spot_routes
        t = getattr(spot_routes, "_SPOT_TIER_TOGGLE", None)
        if t is None:
            return {"A+": True, "A": True, "B": True, "C": True}
        return dict(t.snapshot())
    except Exception:  # noqa: BLE001
        return {"A+": True, "A": True, "B": True, "C": True}


def _latest_scenario(tier: str) -> Optional[dict[str, Any]]:
    try:
        from spot_aggro.governance.scenario_runner import latest_batch_for_tier
        return latest_batch_for_tier(tier)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def authorize_trade(
    symbol: str, side: str, tier: str,
    *,
    source: str = "engine",
    target_proj_wr: float = DEFAULT_TARGET_PROJ_WR,
) -> TradeAuthorization:
    """Run the 12-item Layer 8 checklist for a prospective trade.

    Args:
        symbol: pair like "BTC-USDT"
        side: "buy" or "sell"
        tier: "A+" | "A" | "B" | "C" | "RECON" (reconciled exits bypass
              the usual sample requirements — see below)
        source: freeform tag indicating who triggered the trade
        target_proj_wr: minimum projected WR required (default 0.70)

    Behavior:
        For reconciled exits (source="engine_reconciled_exit"), the
        governor SKIPS the sample-based items and authorizes so the
        engine can safely exit orphan holdings. Those paths are already
        governed separately.
    """
    ts_ms = int(time.time() * 1000)
    authz_id = f"at-{ts_ms}-{uuid.uuid4().hex[:6]}"

    # Safety-exit bypass: any exit driven by engine safety rules
    # (TP/SL/trail/max_hold), a reconciled-position cleanup, or the
    # recon_sweep (which acts on orphan positions using its own
    # threshold logic — live_ret + age + value) is INFORMATIONAL
    # only — we record it, stamp passed=True with an explicit bypass
    # item so the dashboard shows the full trade history, but we never
    # block an exit that is already governed by its own rule set.
    SAFETY_EXIT_SOURCES = (
        "engine_reconciled_exit", "engine_zombie_cleanup",
        "recon_sweep",
    )
    if source.startswith("engine_exit:") or source in SAFETY_EXIT_SOURCES:
        label = ("Safety exit (TP/SL/trail/max_hold)"
                 if source.startswith("engine_exit:")
                 else "Reconciled-position exit")
        detail = (f"source={source} — exit driven by engine safety "
                  "rules; governor records but never blocks")
        az = TradeAuthorization(
            authz_id=authz_id, ts_ms=ts_ms, symbol=symbol, side=side,
            tier=tier, source=source,
            passed=True, score=1.0,
            checklist=[{
                "key": "safety_exit_bypass", "domain": "operational",
                "label": label, "passed": True, "detail": detail,
            }],
            rejection_reason=None,
            target_proj_wr=target_proj_wr,
        )
        _persist(az)
        return az

    from spot_aggro.governance import daily_alpha_gov as gov
    pick, research = _synth_pick(symbol, side, tier)
    scenario = _latest_scenario(tier)
    toggles = _toggle_snapshot()
    verdict = gov.evaluate_pick(
        pick, research=research, scenario=scenario,
        toggles=toggles, target_proj_wr=target_proj_wr,
    )
    az = TradeAuthorization(
        authz_id=authz_id, ts_ms=ts_ms, symbol=symbol, side=side,
        tier=tier, source=source,
        passed=verdict.admitted,
        score=verdict.score,
        checklist=[i.to_dict() if hasattr(i, "to_dict") else dict(i)
                   for i in verdict.checklist],
        rejection_reason=verdict.rejection_reason,
        target_proj_wr=target_proj_wr,
    )
    _persist(az)
    # Phase 11n-9-k: publish blocked buys to Alert Center. The center's
    # 10-min aggregation window means one alert per (symbol, tier) pair
    # no matter how often the engine retries. Auto-upgrades to P1 when
    # occurrences ≥ 5 — the center keeps updating the existing row.
    if (not az.passed) and side == "buy":
        try:
            from spot_aggro.governance import alert_center
            alert_center.ingest(
                kind="pre_trade_gov_block",
                source="pre_trade_gov",
                message=(f"{side.upper()} {symbol} ({tier}) blocked: "
                         f"{az.rejection_reason or 'checklist failed'}"),
                evidence={"symbol": symbol, "tier": tier,
                          "source": source, "score": az.score,
                          "rejection": az.rejection_reason},
            )
        except Exception:  # noqa: BLE001
            pass
    return az


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_pre_trade_authorizations (
    authz_id       TEXT PRIMARY KEY,
    ts_ms          INTEGER NOT NULL,
    symbol         TEXT NOT NULL,
    side           TEXT NOT NULL,
    tier           TEXT NOT NULL,
    source         TEXT NOT NULL,
    passed         INTEGER NOT NULL,
    score          REAL NOT NULL,
    rejection      TEXT,
    payload_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_pretrade_ts
    ON spot_pre_trade_authorizations(ts_ms DESC);
CREATE INDEX IF NOT EXISTS idx_spot_pretrade_symbol
    ON spot_pre_trade_authorizations(symbol);
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


def _persist(az: TradeAuthorization) -> None:
    try:
        _init_schema()
        from shared.persistence import state as persist
        con = persist._connect()
        try:
            con.execute(
                "INSERT OR REPLACE INTO spot_pre_trade_authorizations "
                "(authz_id, ts_ms, symbol, side, tier, source, passed, "
                " score, rejection, payload_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (az.authz_id, az.ts_ms, az.symbol, az.side, az.tier,
                 az.source, 1 if az.passed else 0, az.score,
                 az.rejection_reason,
                 json.dumps(az.to_dict(), default=str)),
            )
            con.commit()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        pass


def latest(limit: int = 20) -> list[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT authz_id, ts_ms, symbol, side, tier, source, "
            " passed, score, rejection "
            "FROM spot_pre_trade_authorizations "
            "ORDER BY ts_ms DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    finally:
        con.close()
    return [
        {"authz_id": r[0], "ts_ms": r[1], "symbol": r[2],
         "side": r[3], "tier": r[4], "source": r[5],
         "passed": bool(r[6]), "score": r[7], "rejection_reason": r[8]}
        for r in rows
    ]


def latest_with_checklist(limit: int = 5) -> list[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT payload_json FROM spot_pre_trade_authorizations "
            "ORDER BY ts_ms DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r[0]))
        except Exception:  # noqa: BLE001
            continue
    return out
