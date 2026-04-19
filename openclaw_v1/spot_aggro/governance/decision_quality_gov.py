"""Phase 11n-9-z — Layer 2: Decision Quality Governor.

Evaluates whether the score-based selection is predictive of outcome
within each (tier, regime, time_bucket) cell. Two tests:

  (1) Decile regression — sort the cell's exits by entry score into 10
      bins. Compute net expectancy per decile. A working scorer has
      the top decile beat the bottom decile; a broken scorer shows
      inversion (top < bottom) — Layer 3 escalates this as T5.

  (2) Rank monotonicity — Spearman ρ of (score rank, net_pnl rank)
      within the cell. ρ >= 0.3 is healthy; 0..0.3 is warn; < 0 is
      confirmed inversion.

Contract:
  * If a cell is score-outcome-inverted on ≥ 3 consecutive ticks, the
    governor calls contradiction_freeze.register_trigger('T5', ...).
  * If Layer 8 pre_trade_authorize is bypassed (engine enters while
    spot_pre_trade_authorizations.passed=0), the governor calls
    contradiction_freeze.register_trigger('T6', ...).
  * GateBypass exception is defined here so the engine's entry path
    can raise it explicitly when the authz lookup indicates a bypass.

Sample-confidence floors inherited from Layer 12:
  cell >= 15 closed exits (below floor → 'insufficient_sample').

Writes to spot_decision_quality_verdicts. Non-enforcing directly — it
feeds Layer 3. Never trades. Never consults capital.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

_DB_LOCK = threading.Lock()

# Minimum sample per cell before rank/decile stats are computed.
CELL_MIN_SAMPLE = 15

# How many consecutive inversion ticks constitute a confirmed inversion
# worth freezing the engine over.
INVERSION_CONSECUTIVE_TICKS = 3


class GateBypass(Exception):
    """Raised when the engine enters a trade that the pre-trade gate
    rejected. This is a hard contract violation — no engine code path
    may place orders without honoring the gate. The exception bubbles
    up to the engine's cycle loop which logs it + triggers Layer 3
    contradiction freeze T6."""

    def __init__(self, authz_id: str | None, symbol: str, detail: str):
        self.authz_id = authz_id
        self.symbol = symbol
        super().__init__(
            f"GateBypass: {symbol} entered despite pre_trade_gate "
            f"passed=0 (authz={authz_id}): {detail}"
        )


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
            con.execute(
                "CREATE TABLE IF NOT EXISTS spot_decision_quality_verdicts("
                " verdict_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts_ms INTEGER NOT NULL,"
                " n_cells INTEGER NOT NULL,"
                " n_cells_healthy INTEGER NOT NULL,"     # ρ >= 0.3
                " n_cells_warn INTEGER NOT NULL,"        # 0 <= ρ < 0.3
                " n_cells_inverted INTEGER NOT NULL,"    # ρ < 0 OR top-decile < bot-decile
                " n_cells_insufficient INTEGER NOT NULL,"
                " overall_verdict TEXT NOT NULL,"        # 'ok' | 'warn' | 'inverted' | 'insufficient'
                " payload_json TEXT NOT NULL"
                ")"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_dq_ts "
                "ON spot_decision_quality_verdicts(ts_ms DESC)"
            )
        finally:
            con.close()


# ---------------------------------------------------------------------------
# Core stats
# ---------------------------------------------------------------------------

def _spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman ρ: Pearson correlation of ranks. Robust to ties."""
    n = len(xs)
    if n < 2:
        return 0.0
    rx = _rank(xs)
    ry = _rank(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = sum((r - mx) ** 2 for r in rx) ** 0.5
    dy = sum((r - my) ** 2 for r in ry) ** 0.5
    return num / (dx * dy) if dx * dy > 0 else 0.0


def _rank(xs: list[float]) -> list[float]:
    """Average-tie ranks."""
    indexed = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and xs[indexed[j + 1]] == xs[indexed[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def _decile_expectancy(scores: list[float], pnls: list[float]) -> tuple[float, float]:
    """Return (top_decile_avg_pnl, bottom_decile_avg_pnl).
    Sort trades by score ascending; bottom 10% = lowest scores."""
    n = len(scores)
    if n < 10:
        return 0.0, 0.0
    pairs = sorted(zip(scores, pnls))
    k = max(1, n // 10)
    bot = pairs[:k]
    top = pairs[-k:]
    bot_avg = sum(p for _, p in bot) / len(bot)
    top_avg = sum(p for _, p in top) / len(top)
    return top_avg, bot_avg


@dataclass
class CellQuality:
    cell_key: str
    n: int
    rho: float                 # Spearman ρ(score, net_pnl)
    top_decile_avg: float
    bottom_decile_avg: float
    decile_inverted: bool      # top < bottom
    verdict: str               # 'ok' | 'warn' | 'inverted' | 'insufficient'
    reason: str


@dataclass
class DecisionQualityVerdict:
    verdict_id: int = 0
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    cells: list[CellQuality] = field(default_factory=list)
    n_cells: int = 0
    n_cells_healthy: int = 0
    n_cells_warn: int = 0
    n_cells_inverted: int = 0
    n_cells_insufficient: int = 0
    overall_verdict: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cells"] = [asdict(c) for c in self.cells]
        return d


def _fetch_exits_with_scores(limit: int = 2000) -> list[sqlite3.Row]:
    """Pull exits joined with entry authorizations to get the score at
    entry time. Falls back to payload_json.score when authz row is
    missing. If spot_pre_trade_authorizations table doesn't exist yet
    (fresh DB), fall back to the payload-only query."""
    con = _connect()
    try:
        # Detect whether the authz table exists.
        has_authz = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            " AND name='spot_pre_trade_authorizations'"
        ).fetchone()[0] > 0
        if has_authz:
            # Primary match: by correlation_id (authz_id). Each exit's
            # payload_json carries an 'authz_id' stamped at entry time;
            # we look that up to read the entry score. If no
            # correlation is recorded, fall back to the nearest-prior
            # authz for the same symbol within 24h.
            return list(con.execute(
                "SELECT tl.ts_ms, tl.symbol, tl.tier, tl.module, tl.net_pnl,"
                " tl.correlation_id, tl.payload_json,"
                " COALESCE("
                "  (SELECT score FROM spot_pre_trade_authorizations a"
                "   WHERE a.authz_id = json_extract(tl.payload_json,"
                "     '$.authz_id') LIMIT 1),"
                "  (SELECT score FROM spot_pre_trade_authorizations a"
                "   WHERE a.symbol = tl.symbol"
                "   AND a.ts_ms <= tl.ts_ms"
                "   AND a.ts_ms >= tl.ts_ms - 86400000"
                "   AND a.side='buy'"
                "   ORDER BY a.ts_ms DESC LIMIT 1)"
                " ) AS entry_score"
                " FROM trade_log tl"
                " WHERE tl.action='exit' AND tl.net_pnl IS NOT NULL"
                " ORDER BY tl.ts_ms DESC LIMIT ?", (limit,),
            ))
        return list(con.execute(
            "SELECT ts_ms, symbol, tier, module, net_pnl,"
            " correlation_id, payload_json, NULL AS entry_score"
            " FROM trade_log"
            " WHERE action='exit' AND net_pnl IS NOT NULL"
            " ORDER BY ts_ms DESC LIMIT ?", (limit,),
        ))
    finally:
        con.close()


def _bucket(row: sqlite3.Row) -> str:
    tier = row["tier"] or "?"
    # Regime from payload if available
    try:
        p = json.loads(row["payload_json"] or "{}")
        regime = p.get("regime") or p.get("regime_at_entry") or "unknown"
    except Exception:
        regime = "unknown"
    hr = time.gmtime((row["ts_ms"] or 0) / 1000).tm_hour
    tb = "00-06" if hr < 6 else "06-12" if hr < 12 else "12-18" if hr < 18 else "18-24"
    return f"{tier}|{regime}|{tb}"


def _extract_score(row: sqlite3.Row) -> float | None:
    if row["entry_score"] is not None:
        return float(row["entry_score"])
    try:
        p = json.loads(row["payload_json"] or "{}")
        s = p.get("score") or p.get("composite")
        return float(s) if s is not None else None
    except Exception:
        return None


def _compute_cell(key: str, rows: list[sqlite3.Row]) -> CellQuality:
    scored = [(r, _extract_score(r)) for r in rows]
    scored = [(r, s) for r, s in scored if s is not None]
    n = len(scored)
    if n < CELL_MIN_SAMPLE:
        return CellQuality(
            cell_key=key, n=n, rho=0.0,
            top_decile_avg=0.0, bottom_decile_avg=0.0,
            decile_inverted=False,
            verdict="insufficient",
            reason=f"n={n} < floor={CELL_MIN_SAMPLE}",
        )
    scores = [s for _, s in scored]
    pnls = [r["net_pnl"] or 0.0 for r, _ in scored]
    rho = _spearman(scores, pnls)
    top, bot = _decile_expectancy(scores, pnls)
    inverted = top < bot
    if inverted or rho < 0:
        verdict = "inverted"
        reason = (f"rho={rho:+.2f}, top_decile={top:+.4f} "
                  f"< bottom_decile={bot:+.4f}" if inverted
                  else f"rho={rho:+.2f} < 0")
    elif rho < 0.3:
        verdict = "warn"
        reason = f"rho={rho:.2f} < 0.3 (weak)"
    else:
        verdict = "ok"
        reason = f"rho={rho:.2f} >= 0.3"
    return CellQuality(
        cell_key=key, n=n, rho=rho,
        top_decile_avg=top, bottom_decile_avg=bot,
        decile_inverted=inverted,
        verdict=verdict, reason=reason,
    )


# ---------------------------------------------------------------------------
# Inversion consecutive-tick tracking
# ---------------------------------------------------------------------------

def _consecutive_inverted_cells(n_ticks: int = INVERSION_CONSECUTIVE_TICKS) -> set[str]:
    """Return cell_keys that have been inverted on the last `n_ticks`
    consecutive decision-quality runs. Used to decide whether to raise
    the T5 contradiction-freeze trigger."""
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT payload_json FROM spot_decision_quality_verdicts"
                " ORDER BY verdict_id DESC LIMIT ?", (n_ticks,),
            ).fetchall()
        finally:
            con.close()
    if len(rows) < n_ticks:
        return set()
    # Intersect the inverted set across all ticks.
    inverted_sets: list[set[str]] = []
    for r in rows:
        try:
            d = json.loads(r["payload_json"] or "{}")
            inv = {
                c["cell_key"] for c in d.get("cells", [])
                if c.get("verdict") == "inverted"
            }
            inverted_sets.append(inv)
        except Exception:
            inverted_sets.append(set())
    persistent = set.intersection(*inverted_sets) if inverted_sets else set()
    return persistent


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_once() -> DecisionQualityVerdict:
    """Single evaluation pass across all (tier, regime, bucket) cells.
    Writes to spot_decision_quality_verdicts. If ≥ 3 consecutive ticks
    show the same cell as inverted, raises Layer 3 T5."""
    _init_schema()
    rows = _fetch_exits_with_scores(limit=2000)
    buckets: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        buckets.setdefault(_bucket(r), []).append(r)
    cells = [_compute_cell(key, grp) for key, grp in buckets.items()]
    v = DecisionQualityVerdict(
        cells=cells,
        n_cells=len(cells),
        n_cells_healthy=sum(1 for c in cells if c.verdict == "ok"),
        n_cells_warn=sum(1 for c in cells if c.verdict == "warn"),
        n_cells_inverted=sum(1 for c in cells if c.verdict == "inverted"),
        n_cells_insufficient=sum(1 for c in cells if c.verdict == "insufficient"),
    )
    if v.n_cells_inverted > 0:
        v.overall_verdict = "inverted"
    elif v.n_cells_warn > 0:
        v.overall_verdict = "warn"
    elif v.n_cells_healthy > 0:
        v.overall_verdict = "ok"
    else:
        v.overall_verdict = "insufficient"
    v.verdict_id = _persist(v)

    # Check for persistent inversion and raise Layer 3 T5
    persistent = _consecutive_inverted_cells()
    if persistent:
        try:
            from spot_aggro.governance.contradiction_freeze import register_trigger
            register_trigger(
                "T5", "ranking_inversion_confirmed",
                f"cells inverted for {INVERSION_CONSECUTIVE_TICKS} ticks: "
                f"{sorted(persistent)}",
            )
        except Exception:
            pass
    return v


def _persist(v: DecisionQualityVerdict) -> int:
    with _DB_LOCK:
        con = _connect()
        try:
            cur = con.execute(
                "INSERT INTO spot_decision_quality_verdicts("
                " ts_ms, n_cells, n_cells_healthy, n_cells_warn,"
                " n_cells_inverted, n_cells_insufficient,"
                " overall_verdict, payload_json)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    v.ts_ms, v.n_cells, v.n_cells_healthy, v.n_cells_warn,
                    v.n_cells_inverted, v.n_cells_insufficient,
                    v.overall_verdict, json.dumps(v.to_dict()),
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
                "SELECT payload_json FROM spot_decision_quality_verdicts"
                " ORDER BY verdict_id DESC LIMIT 1"
            ).fetchone()
            return json.loads(r["payload_json"]) if r else None
        finally:
            con.close()


def history(limit: int = 20) -> list[dict[str, Any]]:
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT verdict_id, ts_ms, n_cells, n_cells_healthy,"
                " n_cells_warn, n_cells_inverted, n_cells_insufficient,"
                " overall_verdict FROM spot_decision_quality_verdicts"
                " ORDER BY verdict_id DESC LIMIT ?", (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


def raise_gate_bypass(authz_id: str | None, symbol: str, detail: str) -> None:
    """Called by engine entry path when it detects a pre-trade gate
    bypass. Registers Layer 3 T6 + raises GateBypass."""
    try:
        from spot_aggro.governance.contradiction_freeze import register_trigger
        register_trigger(
            "T6", "gate_bypass_detected",
            f"{symbol} (authz={authz_id}): {detail}",
        )
    except Exception:
        pass
    raise GateBypass(authz_id, symbol, detail)
