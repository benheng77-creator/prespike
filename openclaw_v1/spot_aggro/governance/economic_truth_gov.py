"""Phase 11n-9-y — Layer 12: Economic Truth Governor.

The missing loop. Every prior governance layer (1-11) audits process:
did the function return, did the endpoint respond, did the card match
the DB. None audit outcome. This layer judges whether the strategy is
actually profitable per cell, using Wilson-bounded expectancy on net PnL.

Segmentation (every metric is computed per cell):
  (symbol, tier, module, regime, time_bucket)

Sample-confidence floors (below floor → warn only, never block):
  symbol        >= 20 closed exits
  tier          >= 50 closed exits
  module        >= 30 closed exits
  regime × cell >= 15 closed exits

Confidence method:
  Wilson 95% on win-rate; expectancy = WR * avg_net_win + (1 - WR) * avg_net_loss.
  "upper" = Wilson upper bound on WR, used when computing optimistic edge.
  "lower" = Wilson lower bound, used when computing pessimistic edge.
  A cell is flagged `fail` when expectancy_wilson_UPPER < 0 — i.e. even
  the most optimistic sampling-error estimate still shows negative edge.

Non-enforcing initially:
  Writes verdicts to spot_economic_truth_verdicts.
  Does NOT flip tier_toggle or write coin_memory.
  Layer 2 (decision quality) + Layer 3 (contradiction freeze) will
  later consume these verdicts to make blocking decisions.

Never places a trade. Never consults capital.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# Sample-confidence floors. Below these, cells are reported with
# verdict='insufficient_sample' and never enter block/fail state.
SAMPLE_FLOOR = {
    "symbol":  20,
    "tier":    50,
    "module":  30,
    "cell":    15,    # regime × (symbol|tier|module) cell
}

# Regimes recognized by spot_aggro_regime_log. Cells on an unknown
# regime pool into 'unknown' rather than being dropped.
_KNOWN_REGIMES = ("trend_up", "trend_dn", "chop", "high_vol", "low_vol", "unknown")

# Time buckets in UTC. Same buckets as operator's 6h-block mental model.
_TIME_BUCKETS = ("00-06", "06-12", "12-18", "18-24")


def _ts_bucket(ts_ms: int) -> str:
    hr = time.gmtime(ts_ms / 1000).tm_hour
    for lo, hi, name in (
        (0, 6, "00-06"), (6, 12, "06-12"),
        (12, 18, "12-18"), (18, 24, "18-24"),
    ):
        if lo <= hr < hi:
            return name
    return "00-06"


# ---------------------------------------------------------------------------
# Wilson interval
# ---------------------------------------------------------------------------

def wilson_wr(wins: int, total: int, z: float = 1.96) -> tuple[float, float, float]:
    """Return (point_wr, lower_95, upper_95) using the Wilson score
    interval. Robust at small n where the normal approximation breaks."""
    if total <= 0:
        return 0.0, 0.0, 0.0
    p = wins / total
    denom = 1 + z * z / total
    center = p + z * z / (2 * total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    lower = max(0.0, (center - margin) / denom)
    upper = min(1.0, (center + margin) / denom)
    return p, lower, upper


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CellStats:
    segment_kind: Literal["tier", "symbol", "module", "cell"]
    segment_key: str
    n: int
    wins: int
    losses: int
    avg_net_win: float
    avg_net_loss: float
    sum_net_pnl: float
    wilson_wr: float          # point estimate
    wilson_wr_lower: float
    wilson_wr_upper: float
    expectancy_point: float
    expectancy_lower: float   # pessimistic: Wilson lower WR + observed avg
    expectancy_upper: float   # optimistic:  Wilson upper WR + observed avg
    rr_point: float           # avg_win / |avg_loss|
    break_even_wr: float      # 1 / (1 + RR)
    verdict: str              # 'ok' | 'warn' | 'fail' | 'insufficient_sample'
    reason: str


@dataclass
class EconomicTruthVerdict:
    run_id: int = 0
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    window_n_exits: int = 0
    cells: list[CellStats] = field(default_factory=list)
    n_cells_ok: int = 0
    n_cells_warn: int = 0
    n_cells_fail: int = 0
    n_cells_insufficient: int = 0
    overall_verdict: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cells"] = [asdict(c) for c in self.cells]
        return d


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_DB_LOCK = threading.Lock()


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
                "CREATE TABLE IF NOT EXISTS spot_economic_truth_verdicts("
                " run_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts_ms INTEGER NOT NULL,"
                " window_n_exits INTEGER NOT NULL,"
                " n_cells_ok INTEGER NOT NULL,"
                " n_cells_warn INTEGER NOT NULL,"
                " n_cells_fail INTEGER NOT NULL,"
                " n_cells_insufficient INTEGER NOT NULL,"
                " overall_verdict TEXT NOT NULL,"
                " payload_json TEXT NOT NULL"
                ")"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_eco_truth_ts "
                "ON spot_economic_truth_verdicts(ts_ms DESC)"
            )
        finally:
            con.close()


def _persist(v: EconomicTruthVerdict) -> int:
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            cur = con.execute(
                "INSERT INTO spot_economic_truth_verdicts("
                " ts_ms, window_n_exits, n_cells_ok, n_cells_warn,"
                " n_cells_fail, n_cells_insufficient, overall_verdict,"
                " payload_json) VALUES(?,?,?,?,?,?,?,?)",
                (
                    v.ts_ms, v.window_n_exits, v.n_cells_ok, v.n_cells_warn,
                    v.n_cells_fail, v.n_cells_insufficient, v.overall_verdict,
                    json.dumps(v.to_dict()),
                ),
            )
            return int(cur.lastrowid or 0)
        finally:
            con.close()


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def _compute_cell(
    *,
    segment_kind: str,
    segment_key: str,
    rows: list[sqlite3.Row],
    sample_floor: int,
) -> CellStats:
    n = len(rows)
    wins = sum(1 for r in rows if (r["net_pnl"] or 0) > 0)
    losses = sum(1 for r in rows if (r["net_pnl"] or 0) < 0)
    win_pnls = [r["net_pnl"] for r in rows if (r["net_pnl"] or 0) > 0]
    loss_pnls = [r["net_pnl"] for r in rows if (r["net_pnl"] or 0) < 0]
    avg_net_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
    avg_net_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0
    sum_net_pnl = sum(r["net_pnl"] or 0 for r in rows)
    wr_point, wr_lower, wr_upper = wilson_wr(wins, wins + losses)
    # Expectancy bounds: pessimistic uses lower-WR + observed avg;
    # optimistic uses upper-WR + observed avg.
    exp_point = wr_point * avg_net_win + (1 - wr_point) * avg_net_loss
    exp_lower = wr_lower * avg_net_win + (1 - wr_lower) * avg_net_loss
    exp_upper = wr_upper * avg_net_win + (1 - wr_upper) * avg_net_loss
    # RR + break-even WR
    rr = abs(avg_net_win / avg_net_loss) if avg_net_loss < 0 else 0.0
    break_even = 1.0 / (1.0 + rr) if rr > 0 else 1.0

    # Verdict
    if n < sample_floor:
        verdict = "insufficient_sample"
        reason = f"n={n} < floor={sample_floor}"
    elif exp_upper < 0:
        # Even optimistic estimate is negative — high-confidence loser
        verdict = "fail"
        reason = (f"exp_upper={exp_upper:+.4f} < 0 "
                  f"(WR={wr_upper*100:.1f}% upper, RR={rr:.2f})")
    elif exp_point < 0:
        # Point estimate negative, but upper bound positive — monitor
        verdict = "warn"
        reason = (f"exp={exp_point:+.4f} < 0 "
                  f"(WR={wr_point*100:.1f}%, RR={rr:.2f}, BE@{break_even*100:.1f}%)")
    else:
        verdict = "ok"
        reason = f"exp={exp_point:+.4f} (WR={wr_point*100:.1f}%, RR={rr:.2f})"

    return CellStats(
        segment_kind=segment_kind,
        segment_key=segment_key,
        n=n, wins=wins, losses=losses,
        avg_net_win=avg_net_win, avg_net_loss=avg_net_loss,
        sum_net_pnl=sum_net_pnl,
        wilson_wr=wr_point, wilson_wr_lower=wr_lower,
        wilson_wr_upper=wr_upper,
        expectancy_point=exp_point,
        expectancy_lower=exp_lower,
        expectancy_upper=exp_upper,
        rr_point=rr, break_even_wr=break_even,
        verdict=verdict, reason=reason,
    )


def _fetch_recent_exits(limit: int = 500) -> list[sqlite3.Row]:
    con = _connect()
    try:
        # net_pnl defaults to 0 on pre-migration rows where backfill
        # hasn't run; we filter those out to avoid polluting the stats.
        return list(con.execute(
            "SELECT ts_ms, symbol, module, tier, net_pnl, pnl_usd,"
            " fee_usd, slippage_usd, payload_json"
            " FROM trade_log"
            " WHERE action='exit' AND net_pnl IS NOT NULL"
            " ORDER BY ts_ms DESC LIMIT ?", (limit,),
        ))
    finally:
        con.close()


def _regime_of(row: sqlite3.Row) -> str:
    """Best-effort regime classification from trade payload, else unknown.
    Avoid joining to spot_aggro_regime_log inline for speed; enrichment
    can be added later without breaking the contract."""
    try:
        p = json.loads(row["payload_json"] or "{}")
        r = p.get("regime") or p.get("regime_at_entry")
        if r and r in _KNOWN_REGIMES:
            return r
    except Exception:
        pass
    return "unknown"


def _bucket_cells(rows: list[sqlite3.Row]) -> dict[tuple, list[sqlite3.Row]]:
    """Group rows by (segment_kind, segment_key). Emit 4 segmentations:
    tier, symbol, module, cell=(regime × tier × time_bucket)."""
    buckets: dict[tuple, list[sqlite3.Row]] = {}
    for r in rows:
        tier = r["tier"] or "?"
        symbol = r["symbol"] or "?"
        module = r["module"] or "?"
        regime = _regime_of(r)
        tbucket = _ts_bucket(r["ts_ms"] or 0)

        buckets.setdefault(("tier", tier), []).append(r)
        buckets.setdefault(("symbol", symbol), []).append(r)
        buckets.setdefault(("module", module), []).append(r)
        cell_key = f"{tier}|{regime}|{tbucket}"
        buckets.setdefault(("cell", cell_key), []).append(r)
    return buckets


def _overall_verdict(cells: list[CellStats]) -> str:
    """One roll-up label. 'fail' dominates, then 'warn', then 'ok'.
    Cells with insufficient_sample don't count toward the verdict."""
    effective = [c for c in cells if c.verdict != "insufficient_sample"]
    if not effective:
        return "insufficient_sample"
    if any(c.verdict == "fail" for c in effective):
        return "fail"
    if any(c.verdict == "warn" for c in effective):
        return "warn"
    return "ok"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_once(*, window: int = 500) -> EconomicTruthVerdict:
    """Compute Wilson-bounded expectancy across all segmentations over
    the most recent `window` closed exits. Non-enforcing: writes verdict
    to spot_economic_truth_verdicts only."""
    rows = _fetch_recent_exits(limit=window)
    buckets = _bucket_cells(rows)
    cells: list[CellStats] = []
    for (kind, key), bucket_rows in buckets.items():
        floor = SAMPLE_FLOOR.get(kind, SAMPLE_FLOOR["cell"])
        cells.append(_compute_cell(
            segment_kind=kind, segment_key=key,
            rows=bucket_rows, sample_floor=floor,
        ))

    v = EconomicTruthVerdict(
        window_n_exits=len(rows),
        cells=cells,
        n_cells_ok=sum(1 for c in cells if c.verdict == "ok"),
        n_cells_warn=sum(1 for c in cells if c.verdict == "warn"),
        n_cells_fail=sum(1 for c in cells if c.verdict == "fail"),
        n_cells_insufficient=sum(1 for c in cells if c.verdict == "insufficient_sample"),
    )
    v.overall_verdict = _overall_verdict(cells)
    try:
        v.run_id = _persist(v)
    except Exception:
        pass
    return v


def latest() -> dict[str, Any] | None:
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            row = con.execute(
                "SELECT payload_json FROM spot_economic_truth_verdicts"
                " ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None
        finally:
            con.close()


def history(limit: int = 20) -> list[dict[str, Any]]:
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT run_id, ts_ms, window_n_exits, n_cells_ok,"
                " n_cells_warn, n_cells_fail, n_cells_insufficient,"
                " overall_verdict"
                " FROM spot_economic_truth_verdicts"
                " ORDER BY run_id DESC LIMIT ?", (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


def fail_cells(verdict: dict[str, Any]) -> list[dict[str, Any]]:
    """Utility: extract cells in verdict=fail state. Layer 2/3 read this
    to feed blocking decisions once enforcement is turned on."""
    return [c for c in verdict.get("cells", []) if c.get("verdict") == "fail"]
