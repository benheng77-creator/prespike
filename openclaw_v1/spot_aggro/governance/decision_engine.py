"""Phase 11n-2 — Decision Engine + conversion-rate telemetry.

Synthesizes every factor the research + scenario + governance layers
produce into a single "why would the system buy, sell, or hold right
now?" answer, with per-factor evidence.

Input factors considered:
  - Per-tier research WR + Wilson CI (from research_agent.latest_report)
  - System-wide overall WR target (default 0.60; SPOT_SYSTEM_WR_TARGET)
  - Per-symbol accuracy rows (from research per_symbol)
  - Scenario Lab best hypothesis per halted tier
  - Research Truth verdict (Layer 4)
  - Card Truth verdict (Layer 5)
  - Daily System Audit verdict (Phase 11j)
  - Tier execution toggles (spot_aggro.gates.tier_toggle)
  - Funnel / recent trade activity (trade_log)

Output per candidate pair:
  DecisionCandidate(
    symbol, tier, action ∈ {buy, hold_long, sell, avoid},
    confidence ∈ [0,1], factors=[list of Factor(name, value, weight, why)],
    target_wr, projected_wr, gates_clear, evidence_refs, reason,
  )

And a ConversionRate snapshot:
  signals_generated, trades_executed, wins, losses, pending
  conversion_rate_pct, win_conversion_pct, loss_conversion_pct

Pure read. No orders placed. No toggles flipped. No capital consulted.
This module is what the dashboard Decision Card reads; the Decision
Truth Governor (decision_truth_gov.py) audits its output.

SPOT AGGRO only. No apex_omega imports.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict, field
from typing import Any, Optional


SYSTEM_WR_TARGET = 0.60
SYSTEM_WR_TARGET_ENV = "SPOT_SYSTEM_WR_TARGET"


def _system_wr_target() -> float:
    import os
    try:
        return float(os.environ.get(SYSTEM_WR_TARGET_ENV, "").strip()
                     or SYSTEM_WR_TARGET)
    except (ValueError, TypeError):
        return SYSTEM_WR_TARGET


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Factor:
    name: str
    value: Any
    weight: float           # 0..1 relative weight in the decision
    why: str                # one-line plain English
    severity: str = "info"  # "info" | "positive" | "warn" | "block"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DecisionCandidate:
    symbol: str
    tier: str
    action: str               # "buy" | "hold_long" | "sell" | "avoid"
    confidence: float         # 0..1
    target_wr: float
    projected_wr: Optional[float]
    gates_clear: bool
    reason: str
    factors: list[Factor]
    evidence_refs: list[str]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["factors"] = [f.to_dict() if hasattr(f, "to_dict") else dict(f)
                        for f in self.factors]
        return d


@dataclass
class ConversionRate:
    signals_generated: int
    trades_executed: int
    wins: int
    losses: int
    pending: int
    conversion_rate_pct: float      # trades / signals
    win_rate_on_trades_pct: float   # wins / (wins + losses)
    signal_to_win_pct: float        # wins / signals

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DecisionBundle:
    generated_ts_ms: int
    system_wr: Optional[float]
    system_wr_target: float
    system_wr_gap: Optional[float]   # target - actual, > 0 means below target
    candidates: list[DecisionCandidate]
    conversion: ConversionRate
    evidence_refs: list[str]
    summary: str                     # one-paragraph human summary

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["candidates"] = [c.to_dict() if hasattr(c, "to_dict") else dict(c)
                           for c in self.candidates]
        d["conversion"] = (self.conversion.to_dict()
                           if hasattr(self.conversion, "to_dict")
                           else dict(self.conversion))
        return d


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------

def _fetch_signals_and_conversion(window_h: float = 24.0) -> ConversionRate:
    """A signal = any 'enter' log row. A trade = any 'enter' that
    subsequently has an 'exit' for the same symbol within the window.
    Pending = enters with no exit yet.
    """
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    try:
        cutoff = int((time.time() - window_h * 3600) * 1000)
        enters = con.execute(
            "SELECT COUNT(*) FROM trade_log "
            "WHERE action='enter' AND ts_ms > ? "
            "AND (module LIKE 'M1_squeeze%' OR module LIKE 'M1_flow%' "
            "     OR module LIKE 'M1_scalp%' OR module LIKE 'M3_blitz%')",
            (cutoff,),
        ).fetchone()[0]
        exits = con.execute(
            "SELECT COUNT(*), "
            " SUM(CASE WHEN pnl_usd > 0.001 THEN 1 ELSE 0 END), "
            " SUM(CASE WHEN pnl_usd < -0.001 THEN 1 ELSE 0 END) "
            "FROM trade_log "
            "WHERE action='exit' AND ts_ms > ? "
            "AND (module LIKE 'M1_squeeze%' OR module LIKE 'M1_flow%' "
            "     OR module LIKE 'M1_scalp%' OR module LIKE 'M3_blitz%')",
            (cutoff,),
        ).fetchone()
    finally:
        con.close()

    exits_n = int(exits[0] or 0)
    wins = int(exits[1] or 0)
    losses = int(exits[2] or 0)
    enters_n = int(enters or 0)
    pending = max(0, enters_n - exits_n)
    conv = (exits_n / enters_n * 100.0) if enters_n > 0 else 0.0
    wr_on_trades = (wins / (wins + losses) * 100.0) if (wins + losses) > 0 else 0.0
    s2w = (wins / enters_n * 100.0) if enters_n > 0 else 0.0
    return ConversionRate(
        signals_generated=enters_n,
        trades_executed=exits_n,
        wins=wins, losses=losses, pending=pending,
        conversion_rate_pct=round(conv, 2),
        win_rate_on_trades_pct=round(wr_on_trades, 2),
        signal_to_win_pct=round(s2w, 2),
    )


def _latest_research() -> Optional[dict[str, Any]]:
    try:
        from spot_aggro.governance.research_agent import latest_report
        return latest_report()
    except Exception:  # noqa: BLE001
        return None


def _latest_scenario(tier: str) -> Optional[dict[str, Any]]:
    try:
        from spot_aggro.governance.scenario_runner import latest_batch_for_tier
        return latest_batch_for_tier(tier)
    except Exception:  # noqa: BLE001
        return None


def _latest_research_truth() -> Optional[dict[str, Any]]:
    try:
        from spot_aggro.governance.research_truth_gov import latest_verdict
        return latest_verdict()
    except Exception:  # noqa: BLE001
        return None


def _latest_card_truth() -> Optional[dict[str, Any]]:
    try:
        from spot_aggro.governance.card_truth_gov import latest_audit
        return latest_audit()
    except Exception:  # noqa: BLE001
        return None


def _latest_system_audit() -> Optional[dict[str, Any]]:
    try:
        from spot_aggro.governance.daily_system_auditor import latest_run
        return latest_run()
    except Exception:  # noqa: BLE001
        return None


def _tier_toggle_snapshot() -> dict[str, bool]:
    try:
        from spot_aggro.api import routes as spot_routes
        tog = getattr(spot_routes, "_SPOT_TIER_TOGGLE", None)
        if tog is None:
            return {"A+": True, "A": True, "B": True, "C": True}
        return dict(tog.snapshot())
    except Exception:  # noqa: BLE001
        return {"A+": True, "A": True, "B": True, "C": True}


# ---------------------------------------------------------------------------
# Decision synthesis
# ---------------------------------------------------------------------------

def _classify_action(
    symbol_row: dict[str, Any],
    tier_wr: Optional[float],
    target: float,
    toggle_on: bool,
    research_valid: bool,
    cards_ok: bool,
) -> tuple[str, float, str]:
    """Return (action, confidence, reason)."""
    # Gating: governance fail-safes must be clean for a buy.
    if not research_valid:
        return ("avoid", 0.1,
                "research truth governor flagged latest report — untrusted inputs")
    if not cards_ok:
        return ("avoid", 0.15,
                "card truth governor flagged dashboard contradictions")
    if not toggle_on:
        return ("hold_long", 0.3,
                f"tier {symbol_row.get('tier')} execution toggle is OFF (operator choice)")
    sym_exits = int(symbol_row.get("exits") or 0)
    sym_wr = symbol_row.get("win_rate")
    # Low data: informational hold.
    if sym_exits < 3:
        return ("hold_long", 0.35,
                f"{symbol_row.get('symbol')} has only {sym_exits} closed exits — "
                f"insufficient sample for a confident call")
    if sym_wr is None:
        return ("avoid", 0.2, "no win-rate computable for this symbol")
    # Strong symbol + tier above target → buy.
    if sym_wr >= target and (tier_wr is None or tier_wr >= target * 0.85):
        return ("buy", min(0.95, 0.5 + sym_wr / 2),
                f"{symbol_row.get('symbol')} WR {sym_wr*100:.0f}% ≥ target "
                f"{target*100:.0f}% across {sym_exits} exits; tier WR "
                f"{(tier_wr or 0)*100:.0f}% supports")
    # Symbol below target → avoid.
    if sym_wr < 0.4:
        return ("avoid", 0.6,
                f"{symbol_row.get('symbol')} WR {sym_wr*100:.0f}% well below "
                f"target {target*100:.0f}% — dragging tier PnL")
    return ("hold_long", 0.45,
            f"{symbol_row.get('symbol')} WR {sym_wr*100:.0f}% borderline — "
            f"hold, re-evaluate next research pass")


def _build_factors(
    symbol_row: dict[str, Any], research: dict[str, Any],
    scenario: Optional[dict[str, Any]], target: float,
    research_valid: bool, cards_ok: bool, system_ok: bool,
    toggle_on: bool,
) -> list[Factor]:
    tier = symbol_row.get("tier", "?")
    tier_stats = {s["tier"]: s for s in (research.get("tier_stats") or [])}
    ts = tier_stats.get(tier, {})
    sym_wr = symbol_row.get("win_rate")
    sym_exits = int(symbol_row.get("exits") or 0)
    tier_wr = ts.get("primary_wr")
    tier_sample = ts.get("primary_sample", 0)
    factors = [
        Factor(
            name="symbol_win_rate",
            value=sym_wr, weight=0.30,
            why=f"{symbol_row.get('symbol')}: {sym_exits} exits, "
                f"WR {(sym_wr or 0)*100:.0f}%",
            severity=("positive" if (sym_wr or 0) >= target
                      else ("warn" if (sym_wr or 0) >= 0.4 else "block")),
        ),
        Factor(
            name="tier_win_rate",
            value=tier_wr, weight=0.25,
            why=f"Tier {tier} WR {(tier_wr or 0)*100:.0f}% across "
                f"{tier_sample} exits",
            severity=("positive" if (tier_wr or 0) >= target else "warn"),
        ),
        Factor(
            name="tier_toggle",
            value=toggle_on, weight=0.15,
            why=("execution allowed" if toggle_on else
                 "execution toggle OFF — operator pause"),
            severity=("positive" if toggle_on else "block"),
        ),
        Factor(
            name="research_truth_gov",
            value=research_valid, weight=0.10,
            why=("research report validated clean" if research_valid
                 else "research truth governor flagged report"),
            severity=("positive" if research_valid else "block"),
        ),
        Factor(
            name="card_truth_gov",
            value=cards_ok, weight=0.10,
            why=("all cards reconcile server-side" if cards_ok
                 else "card truth governor found card-level contradiction"),
            severity=("positive" if cards_ok else "block"),
        ),
        Factor(
            name="system_audit",
            value=system_ok, weight=0.10,
            why=("daily system audit OK" if system_ok
                 else "daily system audit has open findings"),
            severity=("positive" if system_ok else "warn"),
        ),
    ]
    if scenario and scenario.get("best_outcome"):
        bw = scenario["best_outcome"].get("simulated_wr")
        factors.append(Factor(
            name="scenario_best_hypothesis",
            value=bw, weight=0.00,  # informational only
            why=f"scenario lab best simulated WR for tier {tier} = "
                f"{(bw or 0)*100:.0f}%",
            severity=("positive" if (bw or 0) >= target else "info"),
        ))
    return factors


def build_decision_bundle() -> DecisionBundle:
    target = _system_wr_target()
    research = _latest_research() or {}
    rtruth = _latest_research_truth() or {}
    ctruth = _latest_card_truth() or {}
    sys_audit = _latest_system_audit() or {}
    toggles = _tier_toggle_snapshot()

    research_valid = rtruth.get("verdict") in (None, "valid", "suspect")
    # If never run, we treat as "not flagged invalid". That keeps the
    # system advisory-positive until a verdict exists.
    cards_ok = ctruth.get("verdict") in (None, "ok", "warn")
    system_ok = sys_audit.get("verdict") in (None, "ok", "warn")

    per_sym = research.get("per_symbol") or []
    candidates: list[DecisionCandidate] = []
    for row in per_sym:
        tier = row.get("tier", "C")
        toggle_on = bool(toggles.get(tier, True))
        scenario = _latest_scenario(tier)
        tier_stats = {s["tier"]: s for s in (research.get("tier_stats") or [])}
        tier_wr = (tier_stats.get(tier) or {}).get("primary_wr")
        action, conf, reason = _classify_action(
            row, tier_wr=tier_wr, target=target, toggle_on=toggle_on,
            research_valid=research_valid, cards_ok=cards_ok,
        )
        factors = _build_factors(
            row, research, scenario, target,
            research_valid=research_valid, cards_ok=cards_ok,
            system_ok=system_ok, toggle_on=toggle_on,
        )
        projected_wr = None
        if scenario and scenario.get("best_outcome"):
            projected_wr = scenario["best_outcome"].get("simulated_wr")
        gates_clear = research_valid and cards_ok and toggle_on
        candidates.append(DecisionCandidate(
            symbol=row.get("symbol", "?"),
            tier=tier,
            action=action,
            confidence=round(conf, 3),
            target_wr=target,
            projected_wr=projected_wr,
            gates_clear=gates_clear,
            reason=reason,
            factors=factors,
            evidence_refs=[
                f"research://{research.get('report_id','?')}",
                f"truth://{rtruth.get('report_id','?')}",
                f"cards://{ctruth.get('run_id','?')}",
                f"scenario://{scenario.get('batch_id','?') if scenario else 'none'}",
            ],
        ))

    conv = _fetch_signals_and_conversion(24.0)
    system_wr = research.get("overall_wr")
    gap = None if system_wr is None else round(target - system_wr, 4)

    n_buy = sum(1 for c in candidates if c.action == "buy")
    n_avoid = sum(1 for c in candidates if c.action == "avoid")
    n_hold = sum(1 for c in candidates if c.action == "hold_long")
    summary = (
        f"System WR {('—' if system_wr is None else f'{system_wr*100:.1f}%')} "
        f"vs target {target*100:.0f}% · "
        f"{n_buy} buy · {n_hold} hold · {n_avoid} avoid across "
        f"{len(candidates)} symbols · "
        f"conversion {conv.conversion_rate_pct:.1f}% "
        f"({conv.signals_generated}→{conv.trades_executed}), "
        f"{conv.wins}W/{conv.losses}L, {conv.pending} pending."
    )

    return DecisionBundle(
        generated_ts_ms=int(time.time() * 1000),
        system_wr=system_wr,
        system_wr_target=target,
        system_wr_gap=gap,
        candidates=candidates,
        conversion=conv,
        evidence_refs=[
            f"research://{research.get('report_id','?')}",
            f"truth://{rtruth.get('report_id','?')}",
            f"cards://{ctruth.get('run_id','?')}",
            f"system_audit://{sys_audit.get('run_id','?')}",
            "trade_log://enters+exits/24h",
        ],
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Persistence (small, cheap history for the Decision card)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_decision_bundles (
    generated_ts_ms  INTEGER PRIMARY KEY,
    system_wr        REAL,
    system_wr_target REAL NOT NULL,
    n_buy            INTEGER NOT NULL,
    n_hold           INTEGER NOT NULL,
    n_avoid          INTEGER NOT NULL,
    conversion_pct   REAL NOT NULL,
    payload_json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_decisions_ts
    ON spot_decision_bundles(generated_ts_ms DESC);
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


def persist_bundle(b: DecisionBundle) -> None:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        n_buy = sum(1 for c in b.candidates if c.action == "buy")
        n_hold = sum(1 for c in b.candidates if c.action == "hold_long")
        n_avoid = sum(1 for c in b.candidates if c.action == "avoid")
        con.execute(
            "INSERT OR REPLACE INTO spot_decision_bundles "
            "(generated_ts_ms, system_wr, system_wr_target, n_buy, n_hold, "
            " n_avoid, conversion_pct, payload_json) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                b.generated_ts_ms, b.system_wr, b.system_wr_target,
                n_buy, n_hold, n_avoid, b.conversion.conversion_rate_pct,
                json.dumps(b.to_dict(), default=str),
            ),
        )
        con.commit()
    finally:
        con.close()


def latest_bundle() -> Optional[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        row = con.execute(
            "SELECT payload_json FROM spot_decision_bundles "
            "ORDER BY generated_ts_ms DESC LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    return json.loads(row[0]) if row else None


def build_and_persist() -> DecisionBundle:
    b = build_decision_bundle()
    try:
        persist_bundle(b)
    except Exception:  # noqa: BLE001
        pass
    return b
