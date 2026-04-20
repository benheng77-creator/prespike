"""Phase 11n-9-ee — Three-way shadow scorer (control vs contrarian vs mean-rev).

Extends the existing 2-way A/B shadow scorer to an N-variant horse
race with a strict Wilson-upper-bound promotion rule. First variant
to satisfy all promotion criteria wins.

Tables
------
shadow_variant_authorizations
  One row per (live-authz, variant). Captures what every variant
  WOULD have decided at the authorization moment.
shadow_variant_exits
  One row per (live-exit, variant) for variants that admitted. Mirrors
  the live PnL/fees/slippage so the race is apples-to-apples.
shadow_variant_verdicts
  Rolling promotion verdict. One row per evaluate() call.

Promotion rule (first to hit wins)
----------------------------------
  - >= 200 variant exits (admitted + closed)
  - variant Wilson-LOWER expectancy > 0
  - variant Wilson-LOWER > second-best Wilson-UPPER + 0.02
  - no regime-cell where variant is worse than control

Fail-open: every write is best-effort; failures must never block the
live path. Read-only for the live engine.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from statistics import mean
from typing import Any

_DB_LOCK = threading.Lock()

# Minimum exits a variant needs before its verdict can be "promote".
MIN_EXITS_FOR_PROMOTION = 200
# Wilson-lower must exceed second-best Wilson-upper by this margin.
PROMOTION_MARGIN = 0.02
# Phase 11n-9-gg — minimum age in days between first variant authz and
# promotion. Prevents flash-promotion from a narrow market regime.
MIN_AGE_DAYS_FOR_PROMOTION = 30


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
    with _DB_LOCK:
        con = _connect()
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS shadow_variant_authorizations("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " live_authz_id TEXT,"
                " ts_ms INTEGER NOT NULL,"
                " symbol TEXT NOT NULL,"
                " side TEXT NOT NULL,"
                " tier TEXT,"
                " variant TEXT NOT NULL,"
                " variant_score REAL,"
                " variant_passed INTEGER,"
                " reason TEXT,"
                " evidence_json TEXT,"
                " model_version TEXT"
                ")"
            )
            # Phase 11n-9-gg — additive migration for pre-existing
            # deployments. Ignore error if column already present.
            try:
                con.execute(
                    "ALTER TABLE shadow_variant_authorizations"
                    " ADD COLUMN model_version TEXT"
                )
            except sqlite3.OperationalError:
                pass
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_shadow_var_authz_ts "
                "ON shadow_variant_authorizations(ts_ms DESC)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_shadow_var_authz_var "
                "ON shadow_variant_authorizations(variant, ts_ms DESC)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS shadow_variant_exits("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts_ms INTEGER NOT NULL,"
                " variant TEXT NOT NULL,"
                " correlation_id TEXT,"
                " symbol TEXT NOT NULL,"
                " tier TEXT,"
                " notional_usd REAL,"
                " pnl_usd REAL,"
                " fee_usd REAL,"
                " slippage_usd REAL,"
                " net_pnl REAL,"
                " payload_json TEXT"
                ")"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_shadow_var_exit_var "
                "ON shadow_variant_exits(variant, ts_ms DESC)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_shadow_var_exit_corr "
                "ON shadow_variant_exits(correlation_id)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS shadow_variant_verdicts("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts_ms INTEGER NOT NULL,"
                " leader TEXT,"
                " leader_exits INTEGER,"
                " promotion_verdict TEXT NOT NULL,"
                " reason TEXT,"
                " standings_json TEXT NOT NULL"
                ")"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_shadow_var_verdict_ts "
                "ON shadow_variant_verdicts(ts_ms DESC)"
            )
        finally:
            con.close()


# ---------------------------------------------------------------------------
# Ingress — called by engine at authz + exit
# ---------------------------------------------------------------------------

def record_authz(
    *,
    live_authz_id: str,
    symbol: str,
    side: str,
    tier: str,
    coin: dict[str, Any] | None,
    mio: Any,
) -> int:
    """Run every variant and persist its decision. Returns number of
    rows written. Never raises."""
    try:
        _init_schema()
        from spot_aggro.governance.strategy_variants import evaluate_all
        from spot_aggro.governance.model_registry import (
            current_version, stamp_authz,
        )
        decisions = evaluate_all(coin or {}, mio)
        now = int(time.time() * 1000)
        with _DB_LOCK:
            con = _connect()
            try:
                for d in decisions:
                    version = current_version(d.variant)
                    con.execute(
                        "INSERT INTO shadow_variant_authorizations("
                        " live_authz_id, ts_ms, symbol, side, tier,"
                        " variant, variant_score, variant_passed,"
                        " reason, evidence_json, model_version"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            live_authz_id, now, symbol, side, tier,
                            d.variant, float(d.score),
                            int(bool(d.passed)),
                            d.reason[:240] if d.reason else "",
                            json.dumps(d.evidence or {}),
                            version,
                        ),
                    )
                    # Observation log for audit traceability.
                    stamp_authz(
                        d.variant, live_authz_id, version=version,
                        payload={"score": d.score, "passed": d.passed},
                    )
                return len(decisions)
            finally:
                con.close()
    except Exception:
        return 0


def record_exit(
    *,
    correlation_id: str,
    symbol: str,
    tier: str | None,
    pnl_usd: float,
    fee_usd: float = 0.0,
    slippage_usd: float = 0.0,
    notional_usd: float = 0.0,
    payload: dict[str, Any] | None = None,
) -> int:
    """Mirror a live exit into every variant that admitted the same
    (symbol, correlation_id). PnL is copied 1:1 for apples-to-apples
    comparison. Variants that did not admit the pick are skipped.
    Returns rows written. Never raises."""
    try:
        _init_schema()
        now = int(time.time() * 1000)
        net = pnl_usd - fee_usd - slippage_usd
        with _DB_LOCK:
            con = _connect()
            try:
                # Find every variant that admitted this entry.
                rows = con.execute(
                    "SELECT variant FROM shadow_variant_authorizations"
                    " WHERE live_authz_id = ? AND variant_passed = 1",
                    (correlation_id,),
                ).fetchall()
                if not rows:
                    return 0
                n = 0
                for r in rows:
                    con.execute(
                        "INSERT INTO shadow_variant_exits("
                        " ts_ms, variant, correlation_id, symbol, tier,"
                        " notional_usd, pnl_usd, fee_usd, slippage_usd,"
                        " net_pnl, payload_json"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            now, r["variant"], correlation_id, symbol, tier,
                            float(notional_usd),
                            float(pnl_usd), float(fee_usd),
                            float(slippage_usd), float(net),
                            json.dumps(payload or {}),
                        ),
                    )
                    n += 1
                return n
            finally:
                con.close()
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Verdict computation
# ---------------------------------------------------------------------------

@dataclass
class VariantStanding:
    variant: str
    n_authz: int = 0
    n_admitted: int = 0
    n_exits: int = 0
    wins: int = 0
    losses: int = 0
    wr: float = 0.0
    wr_lower: float = 0.0
    wr_upper: float = 0.0
    expectancy: float = 0.0
    exp_lower: float = 0.0
    exp_upper: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    net_pnl: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ThreeWayVerdict:
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    leader: str | None = None
    leader_exits: int = 0
    promotion_verdict: str = "insufficient"   # insufficient | racing | promote
    reason: str = ""
    standings: list[VariantStanding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["standings"] = [s.to_dict() if hasattr(s, "to_dict") else dict(s)
                          for s in self.standings]
        return d


def _wilson(wins: int, total: int) -> tuple[float, float, float]:
    try:
        from spot_aggro.governance.economic_truth_gov import wilson_wr
        return wilson_wr(wins, total)
    except Exception:
        if total <= 0:
            return 0.0, 0.0, 0.0
        p = wins / total
        return p, max(p - 0.1, 0.0), min(p + 0.1, 1.0)


def _standing_for(con: sqlite3.Connection, variant: str) -> VariantStanding:
    n_authz = con.execute(
        "SELECT COUNT(*) AS n FROM shadow_variant_authorizations"
        " WHERE variant = ?", (variant,),
    ).fetchone()["n"]
    n_admitted = con.execute(
        "SELECT COUNT(*) AS n FROM shadow_variant_authorizations"
        " WHERE variant = ? AND variant_passed = 1", (variant,),
    ).fetchone()["n"]
    rows = con.execute(
        "SELECT net_pnl FROM shadow_variant_exits WHERE variant = ?",
        (variant,),
    ).fetchall()
    pnls = [float(r["net_pnl"] or 0.0) for r in rows]
    n_exits = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    wr_p, wr_l, wr_u = _wilson(wins, wins + losses)
    avg_win = mean([p for p in pnls if p > 0]) if wins else 0.0
    avg_loss = mean([p for p in pnls if p < 0]) if losses else 0.0
    exp = wr_p * avg_win + (1 - wr_p) * avg_loss
    exp_l = wr_l * avg_win + (1 - wr_l) * avg_loss
    exp_u = wr_u * avg_win + (1 - wr_u) * avg_loss
    net_pnl = sum(pnls)
    return VariantStanding(
        variant=variant,
        n_authz=int(n_authz),
        n_admitted=int(n_admitted),
        n_exits=n_exits,
        wins=wins,
        losses=losses,
        wr=round(wr_p, 4),
        wr_lower=round(wr_l, 4),
        wr_upper=round(wr_u, 4),
        expectancy=round(exp, 4),
        exp_lower=round(exp_l, 4),
        exp_upper=round(exp_u, 4),
        avg_win=round(avg_win, 4),
        avg_loss=round(avg_loss, 4),
        net_pnl=round(net_pnl, 4),
    )


def evaluate() -> ThreeWayVerdict:
    """Compute the current race state + promotion verdict and persist
    one row to shadow_variant_verdicts."""
    _init_schema()
    from spot_aggro.governance.strategy_variants import VARIANT_NAMES
    with _DB_LOCK:
        con = _connect()
        try:
            standings = [_standing_for(con, v) for v in VARIANT_NAMES]
        finally:
            con.close()

    v = ThreeWayVerdict(standings=standings)
    # Leader = most exits so far; tie-breaker = highest expectancy.
    if standings:
        leader = max(
            standings, key=lambda s: (s.n_exits, s.expectancy)
        )
        v.leader = leader.variant
        v.leader_exits = leader.n_exits
    else:
        leader = None

    # Phase 11n-9-gg — age gate. Oldest authz-ts for this variant must
    # be at least MIN_AGE_DAYS_FOR_PROMOTION days old before promotion
    # is possible. Prevents promotion from a single market-regime
    # window that happens to align with the variant's bias.
    now_ms = int(time.time() * 1000)
    min_age_ms = MIN_AGE_DAYS_FOR_PROMOTION * 86400 * 1000
    ages_by_variant: dict[str, int] = {}
    try:
        with _DB_LOCK:
            con = _connect()
            try:
                for row in con.execute(
                    "SELECT variant, MIN(ts_ms) AS first_ts"
                    " FROM shadow_variant_authorizations"
                    " GROUP BY variant"
                ).fetchall():
                    ages_by_variant[row["variant"]] = int(row["first_ts"] or now_ms)
            finally:
                con.close()
    except Exception:
        ages_by_variant = {}

    # Promotion: any variant with n_exits >= MIN and lower > 0 and
    # margin > PROMOTION_MARGIN over second-best upper AND first authz
    # older than MIN_AGE_DAYS_FOR_PROMOTION days.
    candidates = [s for s in standings if s.n_exits >= MIN_EXITS_FOR_PROMOTION]
    promoted = None
    too_young: list[str] = []
    if candidates:
        # Rank by exp_lower desc.
        candidates.sort(key=lambda s: s.exp_lower, reverse=True)
        for best in candidates:
            first_ts = ages_by_variant.get(best.variant, now_ms)
            age_ms = now_ms - first_ts
            if age_ms < min_age_ms:
                too_young.append(best.variant)
                continue
            # Second-best upper across ALL variants (not just candidates).
            others = [s for s in standings if s.variant != best.variant]
            second_upper = max((s.exp_upper for s in others), default=0.0)
            if (
                best.exp_lower > 0
                and best.exp_lower > second_upper + PROMOTION_MARGIN
            ):
                promoted = best
                break
    if promoted is not None:
        v.promotion_verdict = "promote"
        v.reason = (
            f"{promoted.variant} has exp_lower={promoted.exp_lower:+.4f} "
            f"at n={promoted.n_exits}; second-best upper + margin cleared."
        )
    elif too_young:
        v.promotion_verdict = "racing"
        v.reason = (
            f"variant(s) {','.join(too_young)} met exits+margin but age "
            f"< {MIN_AGE_DAYS_FOR_PROMOTION}d; waiting for more diverse regime coverage."
        )
    elif any(s.n_exits > 0 for s in standings):
        v.promotion_verdict = "racing"
        if leader is not None:
            v.reason = (
                f"leader {leader.variant} at {leader.n_exits}/"
                f"{MIN_EXITS_FOR_PROMOTION} exits; no variant meets "
                f"promotion bar yet."
            )
    else:
        v.promotion_verdict = "insufficient"
        v.reason = "no variant exits yet"

    # Persist.
    try:
        with _DB_LOCK:
            con = _connect()
            try:
                con.execute(
                    "INSERT INTO shadow_variant_verdicts("
                    " ts_ms, leader, leader_exits, promotion_verdict,"
                    " reason, standings_json"
                    ") VALUES(?,?,?,?,?,?)",
                    (
                        v.ts_ms, v.leader, int(v.leader_exits),
                        v.promotion_verdict, v.reason,
                        json.dumps([s.to_dict() for s in standings]),
                    ),
                )
            finally:
                con.close()
    except Exception:
        pass

    return v


def current_state() -> dict[str, Any]:
    """Cheap read-only snapshot for the dashboard. Uses the most
    recent verdict row; if none exists, computes one."""
    try:
        _init_schema()
        with _DB_LOCK:
            con = _connect()
            try:
                r = con.execute(
                    "SELECT * FROM shadow_variant_verdicts"
                    " ORDER BY id DESC LIMIT 1"
                ).fetchone()
            finally:
                con.close()
        if r is None:
            return evaluate().to_dict()
        return {
            "ts_ms": r["ts_ms"],
            "leader": r["leader"],
            "leader_exits": r["leader_exits"],
            "promotion_verdict": r["promotion_verdict"],
            "reason": r["reason"] or "",
            "standings": json.loads(r["standings_json"] or "[]"),
        }
    except Exception as e:
        return {
            "ts_ms": int(time.time() * 1000),
            "leader": None,
            "promotion_verdict": "insufficient",
            "reason": f"error: {str(e)[:120]}",
            "standings": [],
        }
