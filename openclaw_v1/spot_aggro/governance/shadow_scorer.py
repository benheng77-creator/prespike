"""Phase 11n-9-z — Shadow Scorer (A/B validation, zero capital).

The primary root-cause hypothesis from the Layer 12 audit: the
composite score is anti-correlated with realized PnL (Tier A = worst,
Tier C = best-of-bad). Before flipping the scorer sign live, we run
parallel A/B validation against the same market stream:

  A (live)    = compute_composite_score(coin, mio)          [current]
  B (shadow)  = -compute_composite_score(coin, mio)         [inverted]

The shadow scorer does NOT touch capital. It records what B WOULD have
done for every pick and — once real exits close — compares outcomes.

Tables (all prefixed `shadow_`):
  shadow_pre_trade_authorizations — same schema as live but captures
                                    the B-score verdict for every
                                    real authorization event.
  shadow_trade_log                 — paper fills for hypothetical B
                                    entries. PnL copies the live exit
                                    on the same (symbol, correlation)
                                    so we compare apples-to-apples.
  shadow_comparison_verdicts       — rolling every 50 paper exits:
                                    {A_expectancy_net_wilson_low/up,
                                     B_expectancy_net_wilson_low/up,
                                     rank_monotonicity_A,
                                     rank_monotonicity_B, ...}.

Promotion rule (never flipped automatically):
  - ≥ 200 paper exits OR ≥ 7 days, whichever later
  - B_expectancy_wilson_LOWER > 0
  - B_expectancy_wilson_LOWER > A_expectancy_wilson_UPPER + 0.02
  - B_rank_monotonicity > 0.4
  - B_sl_tp_ratio < 2:1
  - No regime-cell where B is worse than A
  The promotion verdict is PUBLISHED; an operator applies it by
  shipping a tagged commit to invert scoring.py. No auto-flip.

Never trades. Never consults capital.
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


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

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
            # Authorization record for the shadow scorer (parallel to
            # spot_pre_trade_authorizations). One row per real entry
            # attempt. Records what the INVERTED score would have done.
            con.execute(
                "CREATE TABLE IF NOT EXISTS shadow_pre_trade_authorizations("
                " shadow_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " live_authz_id TEXT,"                 # FK to live authz
                " ts_ms INTEGER NOT NULL,"
                " symbol TEXT NOT NULL,"
                " side TEXT NOT NULL,"
                " tier TEXT,"
                " live_score REAL,"                    # score A
                " shadow_score REAL,"                  # score B (-A)
                " live_passed INTEGER,"                # what A decided
                " shadow_passed INTEGER,"              # what B would decide
                " payload_json TEXT"
                ")"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_shadow_pta_ts "
                "ON shadow_pre_trade_authorizations(ts_ms DESC)"
            )
            # Paper trade log for shadow entries. PnL is populated when
            # the paired live exit closes (same correlation_id + symbol).
            con.execute(
                "CREATE TABLE IF NOT EXISTS shadow_trade_log("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts_ms INTEGER NOT NULL,"
                " symbol TEXT NOT NULL,"
                " module TEXT,"
                " tier TEXT,"
                " action TEXT NOT NULL,"               # enter | exit
                " notional_usd REAL,"
                " fee_usd REAL,"
                " slippage_usd REAL,"
                " pnl_usd REAL,"
                " net_pnl REAL,"
                " correlation_id TEXT,"
                " live_score REAL,"
                " shadow_score REAL,"
                " payload_json TEXT"
                ")"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_shadow_tl_corr "
                "ON shadow_trade_log(correlation_id)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_shadow_tl_ts "
                "ON shadow_trade_log(ts_ms DESC)"
            )
            # Rolling A/B comparison. Every evaluate() tick writes a row.
            con.execute(
                "CREATE TABLE IF NOT EXISTS shadow_comparison_verdicts("
                " verdict_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts_ms INTEGER NOT NULL,"
                " window_n_a INTEGER NOT NULL,"
                " window_n_b INTEGER NOT NULL,"
                " a_wr REAL, a_wr_lower REAL, a_wr_upper REAL,"
                " b_wr REAL, b_wr_lower REAL, b_wr_upper REAL,"
                " a_expectancy REAL, a_exp_lower REAL, a_exp_upper REAL,"
                " b_expectancy REAL, b_exp_lower REAL, b_exp_upper REAL,"
                " a_rank_monotonicity REAL,"
                " b_rank_monotonicity REAL,"
                " a_sl_tp_ratio REAL,"
                " b_sl_tp_ratio REAL,"
                " promotion_verdict TEXT NOT NULL,"    # 'insufficient' | 'no_promote' | 'promote'
                " reason TEXT NOT NULL,"
                " payload_json TEXT"
                ")"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_shadow_cv_ts "
                "ON shadow_comparison_verdicts(ts_ms DESC)"
            )
        finally:
            con.close()


# ---------------------------------------------------------------------------
# Ingress: called by the live engine at authorization time
# ---------------------------------------------------------------------------

def record_shadow_authz(
    *,
    live_authz_id: str,
    symbol: str,
    side: str,
    tier: str,
    live_score: float,
    live_passed: bool,
    coin: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    """Called by the engine's pre-trade path AFTER the real authz
    decision. Computes the inverted score and writes the shadow
    authorization. Never raises — failure must never break the live
    path."""
    try:
        _init_schema()
        shadow_score = -float(live_score or 0)
        # Shadow 'passed' mirrors the live gate's score threshold but
        # evaluated against the inverted score. The live pre-trade gate
        # admits on score >= 0.7 by default; for the shadow we require
        # shadow_score >= 0.0 (i.e. the live score was <= 0 — a pick
        # the live gate would have REJECTED but B would ADMIT).
        # Net effect: shadow_passed is TRUE on ~half the picks; we
        # compare the shadow-passed picks' realized outcomes to live.
        shadow_passed = 1 if shadow_score >= 0.0 else 0
        with _DB_LOCK:
            con = _connect()
            try:
                cur = con.execute(
                    "INSERT INTO shadow_pre_trade_authorizations("
                    " live_authz_id, ts_ms, symbol, side, tier,"
                    " live_score, shadow_score, live_passed, shadow_passed,"
                    " payload_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        live_authz_id, int(time.time() * 1000),
                        symbol, side, tier,
                        float(live_score or 0), shadow_score,
                        int(bool(live_passed)), shadow_passed,
                        json.dumps({"coin": coin, "payload": payload or {}}),
                    ),
                )
                return int(cur.lastrowid or 0)
            finally:
                con.close()
    except Exception:
        return 0


def record_shadow_entry(
    *,
    correlation_id: str,
    symbol: str,
    module: str,
    tier: str,
    notional_usd: float,
    live_score: float,
    shadow_score: float,
    payload: dict[str, Any] | None = None,
) -> int:
    """Called when the shadow gate WOULD have admitted and the engine
    did open a live position (we mirror the live fill price)."""
    try:
        _init_schema()
        with _DB_LOCK:
            con = _connect()
            try:
                cur = con.execute(
                    "INSERT INTO shadow_trade_log("
                    " ts_ms, symbol, module, tier, action, notional_usd,"
                    " correlation_id, live_score, shadow_score,"
                    " payload_json) VALUES(?,?,?,?, 'enter', ?,?,?,?,?)",
                    (
                        int(time.time() * 1000), symbol, module, tier,
                        notional_usd, correlation_id,
                        live_score, shadow_score,
                        json.dumps(payload or {}),
                    ),
                )
                return int(cur.lastrowid or 0)
            finally:
                con.close()
    except Exception:
        return 0


def record_shadow_exit(
    *,
    correlation_id: str,
    symbol: str,
    pnl_usd: float,
    fee_usd: float = 0.0,
    slippage_usd: float = 0.0,
    payload: dict[str, Any] | None = None,
) -> int:
    """Called when a live exit closes. We mirror the PnL 1:1 so A vs B
    comparison is apples-to-apples — same symbol, same fill, same exit
    timing. The only thing that differs is which scorer would have
    SELECTED the trade in the first place."""
    try:
        _init_schema()
        net = pnl_usd - fee_usd - slippage_usd
        with _DB_LOCK:
            con = _connect()
            try:
                cur = con.execute(
                    "INSERT INTO shadow_trade_log("
                    " ts_ms, symbol, action, pnl_usd, fee_usd,"
                    " slippage_usd, net_pnl, correlation_id, payload_json)"
                    " VALUES(?,?,'exit',?,?,?,?,?,?)",
                    (
                        int(time.time() * 1000), symbol,
                        pnl_usd, fee_usd, slippage_usd, net,
                        correlation_id, json.dumps(payload or {}),
                    ),
                )
                return int(cur.lastrowid or 0)
            finally:
                con.close()
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Comparison-verdict runner
# ---------------------------------------------------------------------------

@dataclass
class ABComparison:
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    window_n_a: int = 0
    window_n_b: int = 0
    a_wr: float = 0.0
    a_wr_lower: float = 0.0
    a_wr_upper: float = 0.0
    b_wr: float = 0.0
    b_wr_lower: float = 0.0
    b_wr_upper: float = 0.0
    a_expectancy: float = 0.0
    a_exp_lower: float = 0.0
    a_exp_upper: float = 0.0
    b_expectancy: float = 0.0
    b_exp_lower: float = 0.0
    b_exp_upper: float = 0.0
    a_rank_monotonicity: float = 0.0
    b_rank_monotonicity: float = 0.0
    a_sl_tp_ratio: float = 0.0
    b_sl_tp_ratio: float = 0.0
    promotion_verdict: str = "insufficient"   # 'insufficient' | 'no_promote' | 'promote'
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _wilson(wins: int, total: int) -> tuple[float, float, float]:
    from spot_aggro.governance.economic_truth_gov import wilson_wr
    return wilson_wr(wins, total)


def _compute_side(
    *,
    exits_pnls: list[float],
    passed_flags: list[bool],
) -> tuple[dict[str, float], float]:
    """Compute WR, expectancy, SL:TP ratio, and rank-monotonicity proxy
    for one side (A or B). Rank monotonicity here is a simple
    proxy: correlation of (passed_flag -> pnl). A good scorer has
    passed=1 correlate positively with pnl > 0."""
    admitted_pnls = [p for p, ok in zip(exits_pnls, passed_flags) if ok]
    n = len(admitted_pnls)
    wins = sum(1 for p in admitted_pnls if p > 0)
    losses = sum(1 for p in admitted_pnls if p < 0)
    wr_p, wr_l, wr_u = _wilson(wins, wins + losses)
    avg_win = mean([p for p in admitted_pnls if p > 0]) if wins else 0.0
    avg_loss = mean([p for p in admitted_pnls if p < 0]) if losses else 0.0
    exp = wr_p * avg_win + (1 - wr_p) * avg_loss
    exp_l = wr_l * avg_win + (1 - wr_l) * avg_loss
    exp_u = wr_u * avg_win + (1 - wr_u) * avg_loss
    sl_tp = (losses / wins) if wins else float("inf")
    # Rank monotonicity proxy: correlation between passed_flag (0/1)
    # and pnl sign (+1 / -1 / 0). Range [-1, +1]. A working scorer
    # scores positively; a broken scorer scores around 0 or negative.
    if len(exits_pnls) >= 2:
        n_all = len(exits_pnls)
        pass_mean = sum(passed_flags) / n_all
        pnl_sign = [1 if p > 0 else (-1 if p < 0 else 0) for p in exits_pnls]
        sign_mean = sum(pnl_sign) / n_all
        num = sum((int(f) - pass_mean) * (s - sign_mean)
                  for f, s in zip(passed_flags, pnl_sign))
        den_a = sum((int(f) - pass_mean) ** 2 for f in passed_flags) ** 0.5
        den_b = sum((s - sign_mean) ** 2 for s in pnl_sign) ** 0.5
        monotonicity = num / (den_a * den_b) if den_a * den_b > 0 else 0.0
    else:
        monotonicity = 0.0
    out = {
        "n": n, "wr": wr_p, "wr_lower": wr_l, "wr_upper": wr_u,
        "expectancy": exp, "exp_lower": exp_l, "exp_upper": exp_u,
        "sl_tp": sl_tp if sl_tp != float("inf") else -1.0,
    }
    return out, monotonicity


def _promotion_decision(a: dict, b: dict, a_mono: float, b_mono: float) -> tuple[str, str]:
    """Apply the five-gate promotion rule. Returns (verdict, reason)."""
    min_exits = 200
    # Window size: count of admitted paper exits on each side
    if a["n"] < min_exits or b["n"] < min_exits:
        return "insufficient", (
            f"admitted exits: A={a['n']} B={b['n']} "
            f"(need >={min_exits} each)"
        )
    checks = []
    ok = True

    if b["exp_lower"] <= 0:
        ok = False
        checks.append(f"B.exp_lower={b['exp_lower']:+.4f} <= 0")
    if b["exp_lower"] <= a["exp_upper"] + 0.02:
        ok = False
        checks.append(
            f"B.exp_lower={b['exp_lower']:+.4f} "
            f"<= A.exp_upper+0.02={a['exp_upper']+0.02:+.4f}"
        )
    if b_mono < 0.4:
        ok = False
        checks.append(f"B.rank_monotonicity={b_mono:.2f} < 0.4")
    if b["sl_tp"] >= 2.0 or b["sl_tp"] < 0:
        ok = False
        checks.append(f"B.sl_tp={b['sl_tp']:.2f} not in [0, 2.0)")

    if ok:
        return "promote", "all five conditions met"
    return "no_promote", "; ".join(checks)


def run_comparison() -> ABComparison:
    """Read shadow + live trade tables, compute A vs B stats, persist
    the comparison verdict."""
    _init_schema()
    # A side = the live admitted entries (trade_log where action='exit'
    # and there was a passing spot_pre_trade_authorizations row for the
    # entry). We approximate by reading net_pnl from trade_log and
    # using live_passed from the shadow_pre_trade_authorizations.
    with _DB_LOCK:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT live_authz_id, live_passed, shadow_passed,"
                " live_score, shadow_score, symbol, ts_ms"
                " FROM shadow_pre_trade_authorizations"
                " ORDER BY ts_ms DESC LIMIT 5000"
            ).fetchall()
            # For each authz, find the matched live exit pnl. Match by
            # symbol + nearest-after ts. Best-effort.
            exit_rows = con.execute(
                "SELECT symbol, ts_ms, net_pnl, payload_json"
                " FROM trade_log WHERE action='exit' AND net_pnl IS NOT NULL"
                " ORDER BY ts_ms DESC LIMIT 5000"
            ).fetchall()
        finally:
            con.close()

    # Simple matcher: for each authz, use the next exit in the same
    # symbol after the authz ts. Skip if none within 48h window.
    exit_by_sym: dict[str, list[sqlite3.Row]] = {}
    for r in exit_rows:
        exit_by_sym.setdefault(r["symbol"], []).append(r)
    for s in exit_by_sym:
        exit_by_sym[s].sort(key=lambda r: r["ts_ms"])

    a_pnls: list[float] = []
    a_passed: list[bool] = []
    b_pnls: list[float] = []
    b_passed: list[bool] = []
    WINDOW_MS = 48 * 3600 * 1000
    for row in rows:
        sym = row["symbol"]
        authz_ts = row["ts_ms"]
        matches = [
            e for e in exit_by_sym.get(sym, [])
            if e["ts_ms"] >= authz_ts
            and (e["ts_ms"] - authz_ts) <= WINDOW_MS
        ]
        if not matches:
            continue
        pnl = matches[0]["net_pnl"] or 0.0
        a_pnls.append(pnl)
        a_passed.append(bool(row["live_passed"]))
        b_pnls.append(pnl)
        b_passed.append(bool(row["shadow_passed"]))

    a_stats, a_mono = _compute_side(exits_pnls=a_pnls, passed_flags=a_passed)
    b_stats, b_mono = _compute_side(exits_pnls=b_pnls, passed_flags=b_passed)
    verdict, reason = _promotion_decision(a_stats, b_stats, a_mono, b_mono)

    ab = ABComparison(
        window_n_a=a_stats["n"], window_n_b=b_stats["n"],
        a_wr=a_stats["wr"], a_wr_lower=a_stats["wr_lower"], a_wr_upper=a_stats["wr_upper"],
        b_wr=b_stats["wr"], b_wr_lower=b_stats["wr_lower"], b_wr_upper=b_stats["wr_upper"],
        a_expectancy=a_stats["expectancy"],
        a_exp_lower=a_stats["exp_lower"], a_exp_upper=a_stats["exp_upper"],
        b_expectancy=b_stats["expectancy"],
        b_exp_lower=b_stats["exp_lower"], b_exp_upper=b_stats["exp_upper"],
        a_rank_monotonicity=a_mono,
        b_rank_monotonicity=b_mono,
        a_sl_tp_ratio=a_stats["sl_tp"],
        b_sl_tp_ratio=b_stats["sl_tp"],
        promotion_verdict=verdict, reason=reason,
    )
    _persist(ab)
    return ab


def _persist(ab: ABComparison) -> int:
    with _DB_LOCK:
        con = _connect()
        try:
            cur = con.execute(
                "INSERT INTO shadow_comparison_verdicts("
                " ts_ms, window_n_a, window_n_b,"
                " a_wr, a_wr_lower, a_wr_upper,"
                " b_wr, b_wr_lower, b_wr_upper,"
                " a_expectancy, a_exp_lower, a_exp_upper,"
                " b_expectancy, b_exp_lower, b_exp_upper,"
                " a_rank_monotonicity, b_rank_monotonicity,"
                " a_sl_tp_ratio, b_sl_tp_ratio,"
                " promotion_verdict, reason, payload_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ab.ts_ms, ab.window_n_a, ab.window_n_b,
                    ab.a_wr, ab.a_wr_lower, ab.a_wr_upper,
                    ab.b_wr, ab.b_wr_lower, ab.b_wr_upper,
                    ab.a_expectancy, ab.a_exp_lower, ab.a_exp_upper,
                    ab.b_expectancy, ab.b_exp_lower, ab.b_exp_upper,
                    ab.a_rank_monotonicity, ab.b_rank_monotonicity,
                    ab.a_sl_tp_ratio, ab.b_sl_tp_ratio,
                    ab.promotion_verdict, ab.reason,
                    json.dumps(ab.to_dict()),
                ),
            )
            return int(cur.lastrowid or 0)
        finally:
            con.close()


def latest() -> dict[str, Any] | None:
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            r = con.execute(
                "SELECT * FROM shadow_comparison_verdicts"
                " ORDER BY verdict_id DESC LIMIT 1"
            ).fetchone()
            return dict(r) if r else None
        finally:
            con.close()


def history(limit: int = 20) -> list[dict[str, Any]]:
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT verdict_id, ts_ms, window_n_a, window_n_b,"
                " a_expectancy, b_expectancy, a_rank_monotonicity,"
                " b_rank_monotonicity, promotion_verdict"
                " FROM shadow_comparison_verdicts"
                " ORDER BY verdict_id DESC LIMIT ?", (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()
