"""Phase 11n-9-vv Path C — per-variant trip-wire + 40-exit promotion.

Two orthogonal rules:

  Safety trip-wire (downside cap per variant):
    if variant's realized live PnL in rolling 24h <= -VARIANT_DD_KILL_USD
    → auto-disable that variant ONLY. Other variants continue.
    Not the same as the global live DD kill ($10 total across all
    variants); this is a per-variant loser ejection.

  40-exit promotion / horse-race verdict:
    first variant to hit N_EXITS_FOR_PROMOTION (40) with
      - wilson_lower > 0    (Wilson-95 LCB positive)
      - net_pnl_usd > 0     (cumulative positive)
    gets verdict "promote".

    Loser after 40 exits with wilson_upper <= 0 gets verdict
    "permanent_disable" — should not trade live again without code change.

Never blocks live path. Pure read-side + advisory writes to
spot_variant_trip_wire_events.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Any

_TRIP_WIRE_WINDOW_H = 24
VARIANT_DD_KILL_USD = float(os.environ.get("SPOT_VARIANT_DD_KILL_USD", "3.0"))
N_EXITS_FOR_PROMOTION = int(os.environ.get("SPOT_PROMO_N_EXITS", "40"))


def _db_path() -> str:
    return (
        os.environ.get("TRADE_DB_PATH")
        or os.environ.get("CLAW_DB_PATH")
        or "trades.db"
    )


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path(), isolation_level=None, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def _init_schema() -> None:
    con = _connect()
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS spot_variant_trip_wire_events("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts_ms INTEGER NOT NULL,"
            " variant TEXT NOT NULL,"
            " kind TEXT NOT NULL,"          # 'dd_kill' | 'promote' | 'permanent_disable'
            " rationale TEXT,"
            " metrics_json TEXT"
            ")"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_vtw_ts"
            " ON spot_variant_trip_wire_events(ts_ms DESC)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_vtw_variant"
            " ON spot_variant_trip_wire_events(variant, kind)"
        )
    finally:
        con.close()


def _wilson_95(wins: int, total: int) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 0.0
    p = wins / total
    z = 1.96
    denom = 1.0 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return max(0.0, center - half), min(1.0, center + half)


@dataclass
class VariantStanding:
    variant: str
    n_exits: int
    wins: int
    losses: int
    wr: float
    wilson_low: float
    wilson_up: float
    net_pnl_usd: float
    pnl_24h_usd: float
    mean_exit_pct: float
    trip_wire_active: bool           # True if variant has been DD-killed
    promotion_verdict: str           # 'racing' | 'promote' | 'permanent_disable' | 'insufficient'
    promotion_reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TripWireReport:
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    variants: list[VariantStanding] = field(default_factory=list)
    n_disabled: int = 0
    n_promoted: int = 0
    dd_kill_threshold_usd: float = VARIANT_DD_KILL_USD
    n_exits_promotion: int = N_EXITS_FOR_PROMOTION

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["variants"] = [
            v.to_dict() if hasattr(v, "to_dict") else dict(v)
            for v in self.variants
        ]
        return d


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def _variant_stats(variant: str) -> dict[str, Any]:
    """Pull variant exit history from spot_live_variant_entries (phase-nn)."""
    _init_schema()
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT notional_usd, realized_pnl_usd, closed_ts_ms"
                " FROM spot_live_variant_entries"
                " WHERE variant = ? AND status = 'closed'"
                " AND realized_pnl_usd IS NOT NULL"
                " ORDER BY closed_ts_ms DESC",
                (variant,),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.OperationalError:
        # Table doesn't exist yet — no closed live variant entries.
        return {
            "n_exits": 0, "wins": 0, "losses": 0,
            "net_pnl_usd": 0.0, "pnl_24h_usd": 0.0,
            "mean_exit_pct": 0.0,
        }
    if not rows:
        return {
            "n_exits": 0, "wins": 0, "losses": 0,
            "net_pnl_usd": 0.0, "pnl_24h_usd": 0.0,
            "mean_exit_pct": 0.0,
        }
    now_ms = int(time.time() * 1000)
    cutoff_24h = now_ms - _TRIP_WIRE_WINDOW_H * 3600 * 1000
    n = len(rows)
    wins = sum(1 for r in rows if float(r["realized_pnl_usd"] or 0) > 0)
    losses = sum(1 for r in rows if float(r["realized_pnl_usd"] or 0) < 0)
    net = sum(float(r["realized_pnl_usd"] or 0) for r in rows)
    net_24h = sum(
        float(r["realized_pnl_usd"] or 0) for r in rows
        if (r["closed_ts_ms"] or 0) >= cutoff_24h
    )
    # Mean % per exit.
    pcts = []
    for r in rows:
        notional = float(r["notional_usd"] or 0)
        pnl = float(r["realized_pnl_usd"] or 0)
        if notional > 0:
            pcts.append(pnl / notional)
    mean_pct = sum(pcts) / len(pcts) if pcts else 0.0
    return {
        "n_exits": n, "wins": wins, "losses": losses,
        "net_pnl_usd": round(net, 4),
        "pnl_24h_usd": round(net_24h, 4),
        "mean_exit_pct": round(mean_pct, 6),
    }


def _is_variant_disabled(variant: str) -> bool:
    """True if variant has a terminal disable event (dd_kill or permanent_disable)."""
    _init_schema()
    try:
        con = _connect()
        try:
            r = con.execute(
                "SELECT COUNT(*) AS n FROM spot_variant_trip_wire_events"
                " WHERE variant = ? AND kind IN ('dd_kill','permanent_disable')",
                (variant,),
            ).fetchone()
        finally:
            con.close()
        return int(r["n"] or 0) > 0
    except Exception:
        return False


def _record_event(variant: str, kind: str, rationale: str,
                  metrics: dict[str, Any]) -> None:
    _init_schema()
    try:
        con = _connect()
        try:
            con.execute(
                "INSERT INTO spot_variant_trip_wire_events("
                " ts_ms, variant, kind, rationale, metrics_json"
                ") VALUES(?,?,?,?,?)",
                (int(time.time() * 1000), variant, kind,
                 rationale[:240], json.dumps(metrics, default=str)),
            )
        finally:
            con.close()
    except Exception:
        pass


def _standing_for(variant: str) -> VariantStanding:
    s = _variant_stats(variant)
    n = s["n_exits"]
    wins = s["wins"]
    losses = s["losses"]
    wr = wins / max(wins + losses, 1) if (wins + losses) > 0 else 0.0
    wilson_l, wilson_u = _wilson_95(wins, wins + losses)

    verdict = "insufficient"
    reason = f"n_exits={n} < {N_EXITS_FOR_PROMOTION}"
    trip_active = _is_variant_disabled(variant)

    if trip_active:
        verdict = "disabled"
        reason = "trip-wire active (permanent disable event recorded)"
    elif n >= N_EXITS_FOR_PROMOTION:
        if wilson_l > 0 and s["net_pnl_usd"] > 0:
            verdict = "promote"
            reason = (
                f"n={n} >= {N_EXITS_FOR_PROMOTION}, Wilson-low "
                f"{wilson_l * 100:.1f}% > 0, net_pnl=${s['net_pnl_usd']:+.2f}"
            )
        elif wilson_u <= 0:
            verdict = "permanent_disable"
            reason = (
                f"n={n} >= {N_EXITS_FOR_PROMOTION}, Wilson-upper "
                f"{wilson_u * 100:.1f}% <= 0 — definitive loser"
            )
        else:
            verdict = "racing"
            reason = (
                f"n={n} reached but Wilson CI spans zero "
                f"({wilson_l * 100:.1f}..{wilson_u * 100:.1f}%); need clearer edge"
            )
    elif n > 0:
        verdict = "racing"
        reason = f"n={n}/{N_EXITS_FOR_PROMOTION} exits (in progress)"

    return VariantStanding(
        variant=variant,
        n_exits=n, wins=wins, losses=losses,
        wr=round(wr, 4),
        wilson_low=round(wilson_l, 4),
        wilson_up=round(wilson_u, 4),
        net_pnl_usd=s["net_pnl_usd"],
        pnl_24h_usd=s["pnl_24h_usd"],
        mean_exit_pct=s["mean_exit_pct"],
        trip_wire_active=trip_active,
        promotion_verdict=verdict,
        promotion_reason=reason,
    )


# ---------------------------------------------------------------------------
# Evaluation / side-effects
# ---------------------------------------------------------------------------

def evaluate(variants: tuple[str, ...] = ("contrarian", "deep_value")
             ) -> TripWireReport:
    """Run one pass: compute standings, fire auto-disable if any
    variant's 24h PnL <= -VARIANT_DD_KILL_USD AND hasn't already been
    disabled. Also records promote / permanent_disable verdicts when
    thresholds crossed."""
    rpt = TripWireReport()
    for v in variants:
        st = _standing_for(v)
        rpt.variants.append(st)
        # Trigger side-effects only on boundary crossings.
        if (
            not st.trip_wire_active
            and st.pnl_24h_usd <= -VARIANT_DD_KILL_USD
        ):
            _record_event(
                variant=v, kind="dd_kill",
                rationale=(
                    f"variant 24h PnL ${st.pnl_24h_usd:+.2f} <= "
                    f"-${VARIANT_DD_KILL_USD} kill"
                ),
                metrics=st.to_dict(),
            )
            st.trip_wire_active = True
            st.promotion_verdict = "disabled"
            st.promotion_reason = "trip-wire just fired (dd_kill)"
            rpt.n_disabled += 1
        elif st.promotion_verdict == "promote":
            rpt.n_promoted += 1
            # Only record once.
            _init_schema()
            try:
                con = _connect()
                try:
                    ex = con.execute(
                        "SELECT COUNT(*) AS n FROM spot_variant_trip_wire_events"
                        " WHERE variant = ? AND kind = 'promote'",
                        (v,),
                    ).fetchone()
                finally:
                    con.close()
                if int(ex["n"] or 0) == 0:
                    _record_event(
                        variant=v, kind="promote",
                        rationale=st.promotion_reason,
                        metrics=st.to_dict(),
                    )
            except Exception:
                pass
        elif st.promotion_verdict == "permanent_disable":
            _init_schema()
            try:
                con = _connect()
                try:
                    ex = con.execute(
                        "SELECT COUNT(*) AS n FROM spot_variant_trip_wire_events"
                        " WHERE variant = ? AND kind = 'permanent_disable'",
                        (v,),
                    ).fetchone()
                finally:
                    con.close()
                if int(ex["n"] or 0) == 0:
                    _record_event(
                        variant=v, kind="permanent_disable",
                        rationale=st.promotion_reason,
                        metrics=st.to_dict(),
                    )
                    rpt.n_disabled += 1
            except Exception:
                pass
    return rpt


def is_variant_disabled(variant: str) -> bool:
    """Public accessor for live_variant_gate. True = strip from enabled set."""
    return _is_variant_disabled(variant)


def enabled_variants_filter(raw_enabled: tuple[str, ...]) -> tuple[str, ...]:
    """Filter the raw SPOT_LIVE_VARIANTS list through disabled events.
    Returned set only contains variants that have NOT been tripped."""
    out: list[str] = []
    for v in raw_enabled:
        if _is_variant_disabled(v):
            continue
        out.append(v)
    return tuple(out)
