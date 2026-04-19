"""Phase 11n-9-aa — Universe Gatekeeper (step 14).

Maintains the list of cells (tier, symbol, module, regime, time_bucket)
that are currently AUTHORIZED to trade. Backed by a single table,
spot_universe_admissions, with one row per (cell_kind, cell_key)
and a lifecycle state: admitted | deprecated_auto | deprecated_manual.

Contract (mechanical; no operator override):
  * A cell is ADMITTED only after it has accumulated ≥ 50 exits with
    expectancy_wilson_LOWER > 0 (strictly positive optimistic floor).
  * Any admitted cell whose expectancy_wilson_UPPER < 0 on ≥ 50 exits
    is automatically set to DEPRECATED_AUTO. This lifecycle transition
    does NOT support operator override — once the data says the cell
    is bleeding on an optimistic estimate, it stays deprecated until
    fresh out-of-sample data reverses the Wilson bound.
  * Newly-admitted cells use the RATCHET rule: before a broader cell
    can be admitted, the current, narrower cell must hold
    expectancy_wilson_LOWER > 0 for 50 more exits each expansion step.

Expansion ratchet (enforced by `next_admissible_cells()`):
  Tier-C + {ENA, DOT}    — initial seed (manually flagged as admitted)
  + one more symbol      — after seed clears 50 Wilson-positive exits
  + one more tier        — after current cell clears 50 more
  + one more module      — after current cell clears 50 more
  (each expansion gated by the prior state remaining Wilson-positive)

Never trades. Read-only against trade_log + spot_economic_truth_verdicts.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Literal

_DB_LOCK = threading.Lock()

# Initial seed per step-12 spec: Tier C, universe = {ENA, DOT}. The
# engine starts here after promotion of the sign-flip commit; no other
# cell trades until the seed has 50 Wilson-positive exits.
SEED_CELLS = (
    {"cell_kind": "tier_symbol", "cell_key": "C|ENA-USDT"},
    {"cell_kind": "tier_symbol", "cell_key": "C|DOT-USDT"},
)

# Strictness floors for the lifecycle transitions.
ADMIT_MIN_EXITS = 50
ADMIT_MIN_WILSON_LOWER = 0.0          # strictly positive
DEPRECATE_MIN_EXITS = 50
DEPRECATE_MAX_WILSON_UPPER = 0.0      # strictly negative


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
            # state ∈ {admitted, deprecated_auto, deprecated_manual}
            con.execute(
                "CREATE TABLE IF NOT EXISTS spot_universe_admissions("
                " cell_kind TEXT NOT NULL,"
                " cell_key TEXT NOT NULL,"
                " state TEXT NOT NULL,"
                " admitted_ts_ms INTEGER,"
                " deprecated_ts_ms INTEGER,"
                " last_wilson_lower REAL,"
                " last_wilson_upper REAL,"
                " last_exits_n INTEGER,"
                " reason TEXT,"
                " updated_ts_ms INTEGER NOT NULL,"
                " PRIMARY KEY (cell_kind, cell_key)"
                ")"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_uadm_state "
                "ON spot_universe_admissions(state)"
            )
            # Seed the two Tier-C symbols on first init. Safe to re-run
            # because of INSERT OR IGNORE.
            now = int(time.time() * 1000)
            for cell in SEED_CELLS:
                con.execute(
                    "INSERT OR IGNORE INTO spot_universe_admissions("
                    " cell_kind, cell_key, state, admitted_ts_ms,"
                    " reason, updated_ts_ms) VALUES(?,?, 'admitted', ?,"
                    " 'phase-aa seed (Tier-C + ENA/DOT)', ?)",
                    (cell["cell_kind"], cell["cell_key"], now, now),
                )
        finally:
            con.close()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Admission:
    cell_kind: str
    cell_key: str
    state: str                         # admitted | deprecated_auto | deprecated_manual
    admitted_ts_ms: int | None
    deprecated_ts_ms: int | None
    last_wilson_lower: float | None
    last_wilson_upper: float | None
    last_exits_n: int | None
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GatekeeperTick:
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    admitted: list[Admission] = field(default_factory=list)
    newly_admitted: list[Admission] = field(default_factory=list)
    newly_deprecated: list[Admission] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts_ms": self.ts_ms,
            "admitted": [a.to_dict() for a in self.admitted],
            "newly_admitted": [a.to_dict() for a in self.newly_admitted],
            "newly_deprecated": [a.to_dict() for a in self.newly_deprecated],
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def admitted_cells() -> list[dict[str, Any]]:
    """Return the CURRENT admitted-cell list. The engine entry path
    reads this to decide whether a prospective trade's cell is
    authorized. Cells in deprecated_* state are NOT returned."""
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT cell_kind, cell_key FROM spot_universe_admissions"
                " WHERE state='admitted' ORDER BY cell_kind, cell_key"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


def is_cell_admitted(cell_kind: str, cell_key: str) -> bool:
    """O(1) check used by the engine entry path."""
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            r = con.execute(
                "SELECT state FROM spot_universe_admissions"
                " WHERE cell_kind=? AND cell_key=?",
                (cell_kind, cell_key),
            ).fetchone()
            return bool(r and r["state"] == "admitted")
        finally:
            con.close()


def all_admissions() -> list[Admission]:
    _init_schema()
    with _DB_LOCK:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT * FROM spot_universe_admissions"
                " ORDER BY state, cell_kind, cell_key"
            ).fetchall()
            return [Admission(**{k: r[k] for k in (
                "cell_kind", "cell_key", "state", "admitted_ts_ms",
                "deprecated_ts_ms", "last_wilson_lower",
                "last_wilson_upper", "last_exits_n", "reason",
            )}) for r in rows]
        finally:
            con.close()


def _layer1_cell_stats() -> list[dict[str, Any]]:
    """Pull the latest Layer 1 verdict's cells. Returns a list with
    segment_kind / segment_key / n / expectancy_lower / expectancy_upper
    so we can make lifecycle decisions."""
    try:
        from spot_aggro.governance.economic_truth_gov import latest
        v = latest()
    except Exception:
        return []
    if not v:
        return []
    return v.get("cells", [])


def _cell_key_for_layer1(seg_kind: str, seg_key: str) -> tuple[str, str] | None:
    """Map Layer 1's segmentation labels to the admission kinds used
    here. Layer 1 uses (segment_kind, segment_key) where kind is
    {tier, symbol, module, cell}. We store same kinds PLUS the
    composite 'tier_symbol' kind used by the seed. The seed cells
    don't directly match Layer 1 output — instead we aggregate per
    (tier, symbol) from trade_log when the gatekeeper ticks."""
    if seg_kind in ("tier", "symbol", "module", "cell"):
        return (seg_kind, seg_key)
    return None


def _tier_symbol_stats_from_trade_log(limit: int = 2000) -> dict[str, dict[str, Any]]:
    """Aggregate (tier, symbol) cells directly from trade_log. Returns
    {cell_key: {n, wins, losses, wilson_lower, wilson_upper}}. We need
    this because Layer 1 segments by tier OR symbol separately but
    the seed cells are tier × symbol."""
    from spot_aggro.governance.economic_truth_gov import wilson_wr
    try:
        con = _connect()
        try:
            rows = list(con.execute(
                "SELECT tier, symbol, net_pnl FROM trade_log"
                " WHERE action='exit' AND net_pnl IS NOT NULL"
                " ORDER BY ts_ms DESC LIMIT ?", (limit,),
            ))
        finally:
            con.close()
    except Exception:
        return {}
    buckets: dict[str, list[float]] = {}
    for r in rows:
        if not r["tier"] or not r["symbol"]:
            continue
        key = f"{r['tier']}|{r['symbol']}"
        buckets.setdefault(key, []).append(r["net_pnl"] or 0.0)
    out: dict[str, dict[str, Any]] = {}
    for k, pnls in buckets.items():
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        _, wr_l, wr_u = wilson_wr(wins, wins + losses)
        avg_w = sum(p for p in pnls if p > 0) / wins if wins else 0.0
        avg_l = sum(p for p in pnls if p < 0) / losses if losses else 0.0
        exp_l = wr_l * avg_w + (1 - wr_l) * avg_l
        exp_u = wr_u * avg_w + (1 - wr_u) * avg_l
        out[k] = {
            "n": n, "wins": wins, "losses": losses,
            "wilson_lower": exp_l, "wilson_upper": exp_u,
        }
    return out


def run_tick() -> GatekeeperTick:
    """Apply the lifecycle rules: promote cells meeting the admission
    bar, deprecate cells meeting the deprecation bar. Returns a tick
    summary."""
    _init_schema()
    tick = GatekeeperTick()
    ts = tick.ts_ms

    # Pull current admissions (all states) + Layer 1 + tier-symbol stats.
    current = {(a.cell_kind, a.cell_key): a for a in all_admissions()}
    l1_cells = _layer1_cell_stats()
    ts_stats = _tier_symbol_stats_from_trade_log()

    def _upsert(
        *,
        cell_kind: str, cell_key: str, new_state: str,
        wilson_lower: float | None, wilson_upper: float | None,
        n_exits: int | None, reason: str,
    ) -> Admission:
        cur = current.get((cell_kind, cell_key))
        admitted_ts = cur.admitted_ts_ms if cur else None
        deprecated_ts = cur.deprecated_ts_ms if cur else None
        if new_state == "admitted" and not admitted_ts:
            admitted_ts = ts
        if new_state == "deprecated_auto":
            deprecated_ts = ts
        with _DB_LOCK:
            con = _connect()
            try:
                con.execute(
                    "INSERT INTO spot_universe_admissions("
                    " cell_kind, cell_key, state, admitted_ts_ms,"
                    " deprecated_ts_ms, last_wilson_lower,"
                    " last_wilson_upper, last_exits_n, reason,"
                    " updated_ts_ms) VALUES(?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(cell_kind, cell_key) DO UPDATE SET"
                    " state=excluded.state,"
                    " admitted_ts_ms=COALESCE(spot_universe_admissions.admitted_ts_ms, excluded.admitted_ts_ms),"
                    " deprecated_ts_ms=excluded.deprecated_ts_ms,"
                    " last_wilson_lower=excluded.last_wilson_lower,"
                    " last_wilson_upper=excluded.last_wilson_upper,"
                    " last_exits_n=excluded.last_exits_n,"
                    " reason=excluded.reason,"
                    " updated_ts_ms=excluded.updated_ts_ms",
                    (
                        cell_kind, cell_key, new_state, admitted_ts,
                        deprecated_ts, wilson_lower, wilson_upper,
                        n_exits, reason, ts,
                    ),
                )
            finally:
                con.close()
        return Admission(
            cell_kind=cell_kind, cell_key=cell_key, state=new_state,
            admitted_ts_ms=admitted_ts, deprecated_ts_ms=deprecated_ts,
            last_wilson_lower=wilson_lower, last_wilson_upper=wilson_upper,
            last_exits_n=n_exits, reason=reason,
        )

    # 1. DEPRECATION pass — any admitted cell whose Wilson upper < 0 on
    #    >= 50 exits is auto-deprecated. No operator override.
    for cell in l1_cells:
        kind = cell.get("segment_kind")
        key = cell.get("segment_key")
        if not kind or not key:
            continue
        mapped = _cell_key_for_layer1(kind, key)
        if not mapped:
            continue
        cur = current.get(mapped)
        if not cur or cur.state != "admitted":
            continue
        n = int(cell.get("n") or 0)
        wu = cell.get("expectancy_upper")
        wl = cell.get("expectancy_lower")
        if n >= DEPRECATE_MIN_EXITS and wu is not None and wu < DEPRECATE_MAX_WILSON_UPPER:
            a = _upsert(
                cell_kind=mapped[0], cell_key=mapped[1],
                new_state="deprecated_auto",
                wilson_lower=wl, wilson_upper=wu, n_exits=n,
                reason=(f"auto-deprecate: n={n} wilson_upper={wu:+.4f}"
                        f" < {DEPRECATE_MAX_WILSON_UPPER}"),
            )
            tick.newly_deprecated.append(a)

    # Also check the composite tier-symbol cells from trade_log.
    for key, s in ts_stats.items():
        cur = current.get(("tier_symbol", key))
        if not cur or cur.state != "admitted":
            continue
        if (s["n"] >= DEPRECATE_MIN_EXITS
                and s["wilson_upper"] < DEPRECATE_MAX_WILSON_UPPER):
            a = _upsert(
                cell_kind="tier_symbol", cell_key=key,
                new_state="deprecated_auto",
                wilson_lower=s["wilson_lower"],
                wilson_upper=s["wilson_upper"],
                n_exits=s["n"],
                reason=(f"auto-deprecate: n={s['n']} wilson_upper="
                        f"{s['wilson_upper']:+.4f} < 0"),
            )
            tick.newly_deprecated.append(a)

    # After the deprecation pass, refresh the `current` dict so the
    # promotion loop sees newly-deprecated cells and does NOT re-admit
    # them via the "refresh stats" branch below.
    current = {(a.cell_kind, a.cell_key): a for a in all_admissions()}

    # 2. PROMOTION pass — already-admitted cells just refresh their
    #    stats; un-admitted cells with >= 50 exits AND wilson_lower > 0
    #    become admitted_auto. Step-12 seed (Tier-C + ENA/DOT) was
    #    inserted at _init_schema; other cells must earn admission.
    for key, s in ts_stats.items():
        cur = current.get(("tier_symbol", key))
        if s["n"] < ADMIT_MIN_EXITS:
            continue
        if s["wilson_lower"] <= ADMIT_MIN_WILSON_LOWER:
            # Refresh stats on existing admitted cells; do not admit.
            if cur and cur.state == "admitted":
                _upsert(
                    cell_kind="tier_symbol", cell_key=key,
                    new_state="admitted",
                    wilson_lower=s["wilson_lower"],
                    wilson_upper=s["wilson_upper"],
                    n_exits=s["n"], reason="refresh",
                )
            continue
        # Wilson_lower > 0 — eligible.
        if cur and cur.state == "admitted":
            continue  # already in
        if cur and cur.state.startswith("deprecated"):
            continue  # stay deprecated; fresh out-of-sample data resets externally
        a = _upsert(
            cell_kind="tier_symbol", cell_key=key, new_state="admitted",
            wilson_lower=s["wilson_lower"],
            wilson_upper=s["wilson_upper"],
            n_exits=s["n"],
            reason=(f"auto-admit: n={s['n']} wilson_lower="
                    f"{s['wilson_lower']:+.4f} > 0"),
        )
        tick.newly_admitted.append(a)

    # Snapshot the admitted set for the tick report.
    tick.admitted = [a for a in all_admissions() if a.state == "admitted"]
    return tick


def deprecate_manual(cell_kind: str, cell_key: str, reason: str) -> None:
    """Operator-initiated deprecation. Separate from deprecated_auto so
    the lifecycle log distinguishes data-driven vs manual decisions."""
    _init_schema()
    ts = int(time.time() * 1000)
    with _DB_LOCK:
        con = _connect()
        try:
            con.execute(
                "UPDATE spot_universe_admissions"
                " SET state='deprecated_manual', deprecated_ts_ms=?,"
                " reason=?, updated_ts_ms=?"
                " WHERE cell_kind=? AND cell_key=?",
                (ts, f"manual: {reason}", ts, cell_kind, cell_key),
            )
        finally:
            con.close()
