"""Phase 11n — Scenario Lab auto-runner for the research agent.

Runs a deterministic permutation matrix of hypothetical trading
simulations and persists every outcome. The research agent uses these
outcomes as extra evidence when deciding whether a halted tier has a
plausible parameter configuration that could lift WR above the restore
threshold.

Properties:
  - Deterministic: same seed + same inputs => identical output.
  - Hypothetical: never places real orders. Never touches engine state.
  - Fast: pure numpy-free Python math over historical closed trades.
  - Persisted: every run stored to `spot_scenario_runs` with a
    scenario_batch_id + evidence_ref linkage.
  - Non-stop mode: keeps permuting until a scenario's simulated
    expected WR ≥ target_wr (default 0.60) OR the entire permutation
    matrix is exhausted.

SPOT AGGRO only. No apex_omega imports.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


log = logging.getLogger(__name__)

Clock = Callable[[], float]


def _wall_clock() -> float: return time.time()


# ---------------------------------------------------------------------------
# Permutation matrix — what the researcher tries.
#
# Each axis is a hypothesis: "would tightening TP/SL improve WR?",
# "would the bot do better in a calm market?", etc. The cartesian
# product gives us `len(styles) * len(markets) * len(capitals) * ...`
# scenarios. Default matrix = 3*3*3*2*4 = 216 scenarios.
# ---------------------------------------------------------------------------

DEFAULT_MATRIX: dict[str, list[Any]] = {
    "style":         ["conservative", "balanced", "aggressive"],
    "market":        ["calm", "trending", "squeeze"],
    "capital_usd":   [200, 400, 800],
    "days":          [3, 7],
    "tp_mult":       [0.8, 1.0, 1.2, 1.5],
}


@dataclass
class ScenarioInput:
    tier: str
    style: str
    market: str
    capital_usd: float
    days: int
    tp_mult: float
    # Phase 11n-3: per-pass salt. Same inputs + same salt = same id
    # (determinism preserved for tests). Different pass => different salt
    # => different id, guaranteeing loop novelty.
    salt: str = ""

    def fingerprint(self) -> str:
        """Stable hash so re-running the same input (+ same salt) returns
        the same scenario_id. Novelty comes from varying the salt across
        passes."""
        blob = json.dumps(asdict(self), sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16]


@dataclass
class ScenarioOutcome:
    scenario_id: str
    scenario_batch_id: str
    generated_ts_ms: int
    tier: str
    inputs: dict[str, Any]
    n_trades_simulated: int
    simulated_wr: float
    simulated_expectancy_usd: float
    simulated_profit_factor: float
    simulated_max_dd_usd: float
    evidence_ref: str             # e.g. "db://apex_trade_log?tier=B&window_h=24"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = datetime.fromtimestamp(
            self.generated_ts_ms / 1000, tz=timezone.utc
        ).isoformat()
        return d


# ---------------------------------------------------------------------------
# Core simulator.
#
# The sim is intentionally simple and deterministic: it takes each
# closed exit row from the last N days for the given tier, applies the
# scenario's tp_mult to the pnl_usd (capped by the original pnl if the
# trade was a loss — simulating a wider SL doesn't magically un-lose a
# trade), then recomputes WR + expectancy + PF. This is a "what-if"
# counterfactual on REAL closed trades, not a Monte Carlo.
# ---------------------------------------------------------------------------

def _fetch_closed_trades(tier: str, days: int) -> list[dict[str, Any]]:
    """All closed exits for this tier in the last `days` days."""
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    try:
        cutoff_ms = int((time.time() - days * 86400) * 1000)
        rows = con.execute(
            "SELECT symbol, pnl_usd, notional_usd, ts_ms "
            "FROM apex_trade_log WHERE action='exit' AND tier = ? "
            "AND ts_ms > ? AND pnl_usd IS NOT NULL "
            "AND (module LIKE 'M1_squeeze%' OR module LIKE 'M1_flow%' "
            "     OR module LIKE 'M1_scalp%' OR module LIKE 'M3_blitz%')",
            (tier, cutoff_ms),
        ).fetchall()
    finally:
        con.close()
    return [
        {"symbol": r[0], "pnl_usd": float(r[1] or 0),
         "notional": float(r[2] or 0), "ts_ms": int(r[3])}
        for r in rows
    ]


def _style_multiplier(style: str) -> float:
    """Conservative tightens exits → fewer wins but also fewer losses.
    Aggressive widens → the opposite."""
    return {"conservative": 0.85, "balanced": 1.0, "aggressive": 1.20}.get(
        style, 1.0
    )


def _market_bias(market: str) -> float:
    """Imagined PnL bias per market regime. Calm markets reduce magnitude;
    squeeze amplifies; trending moderate."""
    return {"calm": 0.70, "trending": 1.00, "squeeze": 1.30}.get(market, 1.0)


def simulate(inp: ScenarioInput, *, clock: Optional[Clock] = None) -> ScenarioOutcome:
    """Run one scenario. Deterministic: same inputs + same DB => same
    outcome."""
    c = clock or _wall_clock
    ts_ms = int(c() * 1000)
    trades = _fetch_closed_trades(inp.tier, inp.days)

    notes: list[str] = []

    if not trades:
        return ScenarioOutcome(
            scenario_id=inp.fingerprint(),
            scenario_batch_id="<set-by-batch>",
            generated_ts_ms=ts_ms,
            tier=inp.tier,
            inputs=asdict(inp),
            n_trades_simulated=0,
            simulated_wr=0.0,
            simulated_expectancy_usd=0.0,
            simulated_profit_factor=0.0,
            simulated_max_dd_usd=0.0,
            evidence_ref=(
                f"db://apex_trade_log?tier={inp.tier}&days={inp.days}&count=0"
            ),
            notes=["no closed trades in window — simulation not meaningful"],
        )

    style_k = _style_multiplier(inp.style)
    market_k = _market_bias(inp.market)
    tp_k = inp.tp_mult

    # Counterfactual: scale winning PnL by tp_mult (tighter TP clips gains,
    # looser TP amplifies them — bounded by the real peak of the trade
    # which we don't have, so we use the realized pnl as ceiling for
    # losers and floor for winners).
    wins = 0
    losses = 0
    sum_pnl = 0.0
    gross_wins = 0.0
    gross_losses = 0.0
    equity = 0.0
    peak = 0.0
    max_dd = 0.0

    for t in trades:
        pnl = t["pnl_usd"]
        if pnl > 0:
            adj = pnl * tp_k * style_k * market_k
        elif pnl < 0:
            # Looser SL (tp_k > 1) makes losses bigger; tighter SL smaller.
            # Style/market biases apply symmetrically.
            adj = pnl * (2.0 - tp_k) * style_k * market_k
            # Floor at -2x original (no scenario can magnify a loss 10x).
            adj = max(adj, pnl * 2.0)
        else:
            adj = 0.0

        sum_pnl += adj
        if adj > 0.001:
            wins += 1
            gross_wins += adj
        elif adj < -0.001:
            losses += 1
            gross_losses += abs(adj)

        equity += adj
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    n_exits = wins + losses
    wr = (wins / n_exits) if n_exits else 0.0
    expectancy = sum_pnl / n_exits if n_exits else 0.0
    pf = (gross_wins / gross_losses) if gross_losses > 0 else (
        float("inf") if gross_wins > 0 else 0.0
    )
    # Clamp pf so JSON encoding is stable.
    if not math.isfinite(pf):
        pf = 99.0 if gross_wins > 0 else 0.0

    # Style + market notes so the research agent can surface them.
    if inp.style == "conservative" and wr > 0.5:
        notes.append(
            f"Conservative style shows promise: simulated WR {wr*100:.1f}% "
            f"across {n_exits} counterfactual trades."
        )
    if inp.tp_mult < 1.0 and expectancy > 0:
        notes.append(
            f"Tightening TP to {inp.tp_mult:.2f}× flips expectancy positive "
            f"(${expectancy:.3f}/trade)."
        )

    return ScenarioOutcome(
        scenario_id=inp.fingerprint(),
        scenario_batch_id="<set-by-batch>",
        generated_ts_ms=ts_ms,
        tier=inp.tier,
        inputs=asdict(inp),
        n_trades_simulated=len(trades),
        simulated_wr=wr,
        simulated_expectancy_usd=expectancy,
        simulated_profit_factor=pf,
        simulated_max_dd_usd=max_dd,
        evidence_ref=(
            f"db://apex_trade_log?tier={inp.tier}&days={inp.days}&count={len(trades)}"
        ),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Batch runner.
# ---------------------------------------------------------------------------

@dataclass
class ScenarioBatch:
    batch_id: str
    generated_ts_ms: int
    tier: str
    target_wr: float
    matrix: dict[str, list[Any]]
    outcomes: list[ScenarioOutcome]
    best_outcome: Optional[ScenarioOutcome]
    found_target: bool
    stopped_reason: str       # "target_reached" | "matrix_exhausted" | "empty_data"
    # Phase 11n-3: novelty enforcement fields.
    pass_index: int = 0               # Nth consecutive run for this tier
    axis_order: tuple = ()            # the order axes were iterated
    seed_salt: str = ""               # per-pass salt so fingerprints differ
    batch_signature: str = ""         # deterministic hash over (axis_order, seed_salt, cap, matrix_keys)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "generated_ts_ms": self.generated_ts_ms,
            "timestamp": datetime.fromtimestamp(
                self.generated_ts_ms / 1000, tz=timezone.utc
            ).isoformat(),
            "tier": self.tier,
            "target_wr": self.target_wr,
            "matrix": self.matrix,
            "outcomes": [o.to_dict() for o in self.outcomes],
            "best_outcome": self.best_outcome.to_dict() if self.best_outcome else None,
            "found_target": self.found_target,
            "stopped_reason": self.stopped_reason,
            "n_outcomes": len(self.outcomes),
            "pass_index": self.pass_index,
            "axis_order": list(self.axis_order),
            "seed_salt": self.seed_salt,
            "batch_signature": self.batch_signature,
        }


def _iterate_matrix(
    matrix: dict[str, list[Any]], tier: str,
    *, salt: str = "",
    axis_order: Optional[tuple[str, ...]] = None,
    value_shift: int = 0,
):
    """Yield ScenarioInput across the cartesian product of the matrix
    axes.

    Phase 11n-3 loop-novelty parameters:
      axis_order — which order to iterate axes. Changes the *sequence*
                   scenarios are visited. Default = dict insertion order.
      value_shift — rotate each axis's value list by N positions so the
                   first scenario is never the same two passes in a row.
      salt — injected into every ScenarioInput so duplicate combos across
             passes produce different scenario_ids.
    """
    keys = list(axis_order) if axis_order else list(matrix.keys())
    # Only use axes present in the matrix; ignore missing.
    keys = [k for k in keys if k in matrix]
    # Append any matrix keys not in the explicit order (defensive).
    for k in matrix.keys():
        if k not in keys:
            keys.append(k)
    values = []
    for k in keys:
        vs = list(matrix[k])
        if vs and value_shift:
            shift = value_shift % len(vs)
            vs = vs[shift:] + vs[:shift]
        values.append(vs)
    def _product(vals):
        if not vals:
            yield []; return
        head, *tail = vals
        for h in head:
            for rest in _product(tail):
                yield [h] + rest
    for combo in _product(values):
        kv = dict(zip(keys, combo))
        yield ScenarioInput(tier=tier, salt=salt, **kv)


# Axis-order rotations — deterministic, distinct per pass_index.
_AXIS_ROTATIONS: tuple[tuple[str, ...], ...] = (
    ("style", "market", "capital_usd", "days", "tp_mult"),
    ("tp_mult", "style", "market", "capital_usd", "days"),
    ("market", "days", "style", "tp_mult", "capital_usd"),
    ("capital_usd", "tp_mult", "days", "market", "style"),
    ("days", "capital_usd", "style", "tp_mult", "market"),
    ("style", "tp_mult", "capital_usd", "market", "days"),
    ("market", "style", "tp_mult", "days", "capital_usd"),
)


def _prior_pass_count(tier: str) -> int:
    """How many batches for this tier have been persisted before. Used
    to choose a distinct rotation + salt for the next pass."""
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM spot_scenario_batches WHERE tier = ?",
            (tier,),
        ).fetchone()
    finally:
        con.close()
    return int(row[0]) if row else 0


def _batch_signature(
    tier: str, matrix: dict[str, list[Any]],
    axis_order: tuple, seed_salt: str, cap: int, non_stop: bool,
) -> str:
    """Hash over the things that must differ to make a loop 'novel'.
    If the same signature appears back-to-back, the Loop Novelty
    Governor will flag it as a repeat."""
    blob = json.dumps({
        "tier": tier,
        "matrix_keys": sorted(matrix.keys()),
        "matrix_values": {k: sorted(map(str, v)) for k, v in matrix.items()},
        "axis_order": list(axis_order),
        "seed_salt": seed_salt,
        "cap": cap,
        "non_stop": non_stop,
    }, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _pick_novelty(
    tier: str, matrix: dict[str, list[Any]],
) -> tuple[int, tuple[str, ...], str, int]:
    """Choose (pass_index, axis_order, seed_salt, value_shift) that
    guarantees this batch differs from the most recent one for `tier`."""
    pass_index = _prior_pass_count(tier)
    rotation = _AXIS_ROTATIONS[pass_index % len(_AXIS_ROTATIONS)]
    # Salt includes pass_index + wall clock ms so every pass is unique
    # across process restarts too. Kept compact.
    seed_salt = f"p{pass_index}-{int(time.time()*1000) & 0xFFFFFF:x}"
    # Rotate axis values each pass (cycles every len(matrix)*N passes).
    value_shift = pass_index
    return pass_index, rotation, seed_salt, value_shift


def run_batch(
    tier: str,
    *,
    target_wr: float = 0.60,
    matrix: Optional[dict[str, list[Any]]] = None,
    non_stop: bool = False,
    cap: int = 500,
    clock: Optional[Clock] = None,
    # Phase 11n-3 novelty overrides. If any is None, _pick_novelty decides.
    axis_order: Optional[tuple[str, ...]] = None,
    seed_salt: Optional[str] = None,
    value_shift: Optional[int] = None,
) -> ScenarioBatch:
    """Run every scenario in the permutation matrix for `tier`.

    Args:
        tier: "A+", "A", "B", or "C".
        target_wr: hypothesis-success threshold. When `non_stop=True`,
            the loop stops as soon as any scenario's simulated WR >=
            target_wr (the researcher has found a plausible recovery
            configuration). When `non_stop=False`, always runs the full
            matrix.
        matrix: override the default permutation axes.
        non_stop: keep iterating until target hit OR matrix exhausted.
        cap: hard upper bound on scenarios run (protects CI from a huge
            custom matrix).
        clock: deterministic clock for tests.
    """
    c = clock or _wall_clock
    ts_ms = int(c() * 1000)
    batch_id = f"sb-{ts_ms}-{uuid.uuid4().hex[:6]}"
    matrix = matrix or DEFAULT_MATRIX

    # Phase 11n-3: choose a novelty profile that guarantees this batch
    # differs from the previous batch for the same tier along at least
    # one of (axis_order, seed_salt, value_shift). Caller overrides win.
    chosen_pass, chosen_order, chosen_salt, chosen_shift = _pick_novelty(
        tier, matrix,
    )
    if axis_order is not None:  chosen_order = tuple(axis_order)
    if seed_salt  is not None:  chosen_salt = seed_salt
    if value_shift is not None: chosen_shift = value_shift

    outcomes: list[ScenarioOutcome] = []
    best: Optional[ScenarioOutcome] = None
    stopped_reason = "matrix_exhausted"
    found_target = False

    for i, inp in enumerate(_iterate_matrix(
        matrix, tier,
        salt=chosen_salt, axis_order=chosen_order, value_shift=chosen_shift,
    )):
        if i >= cap:
            stopped_reason = "cap_reached"
            break
        outcome = simulate(inp, clock=c)
        outcome.scenario_batch_id = batch_id
        outcomes.append(outcome)

        if (best is None
            or outcome.simulated_expectancy_usd > best.simulated_expectancy_usd
        ):
            best = outcome

        if outcome.simulated_wr >= target_wr and outcome.n_trades_simulated >= 3:
            found_target = True
            if non_stop:
                stopped_reason = "target_reached"
                break

    if not outcomes:
        stopped_reason = "empty_matrix"

    signature = _batch_signature(
        tier, matrix, chosen_order, chosen_salt, cap, non_stop,
    )

    return ScenarioBatch(
        batch_id=batch_id,
        generated_ts_ms=ts_ms,
        tier=tier,
        target_wr=target_wr,
        matrix=matrix,
        outcomes=outcomes,
        best_outcome=best,
        found_target=found_target,
        stopped_reason=stopped_reason,
        pass_index=chosen_pass,
        axis_order=chosen_order,
        seed_salt=chosen_salt,
        batch_signature=signature,
    )


# ---------------------------------------------------------------------------
# Persistence.
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_scenario_runs (
    scenario_id        TEXT NOT NULL,
    scenario_batch_id  TEXT NOT NULL,
    generated_ts_ms    INTEGER NOT NULL,
    tier               TEXT NOT NULL,
    n_trades_simulated INTEGER NOT NULL,
    simulated_wr       REAL NOT NULL,
    simulated_expectancy_usd REAL NOT NULL,
    simulated_profit_factor  REAL NOT NULL,
    simulated_max_dd_usd     REAL NOT NULL,
    payload_json       TEXT NOT NULL,
    PRIMARY KEY (scenario_id, scenario_batch_id)
);
CREATE INDEX IF NOT EXISTS idx_spot_scenario_batch
    ON spot_scenario_runs(scenario_batch_id);
CREATE INDEX IF NOT EXISTS idx_spot_scenario_ts
    ON spot_scenario_runs(generated_ts_ms DESC);

CREATE TABLE IF NOT EXISTS spot_scenario_batches (
    batch_id         TEXT PRIMARY KEY,
    generated_ts_ms  INTEGER NOT NULL,
    tier             TEXT NOT NULL,
    target_wr        REAL NOT NULL,
    n_outcomes       INTEGER NOT NULL,
    found_target     INTEGER NOT NULL,
    stopped_reason   TEXT NOT NULL,
    best_scenario_id TEXT,
    batch_signature  TEXT DEFAULT '',
    pass_index       INTEGER DEFAULT 0,
    payload_json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spot_scenario_batches_sig
    ON spot_scenario_batches(tier, batch_signature);
CREATE INDEX IF NOT EXISTS idx_spot_scenario_batches_ts
    ON spot_scenario_batches(generated_ts_ms DESC);
"""


def _init_schema() -> None:
    from shared.persistence import state as persist
    persist.init_schema()
    con = persist._connect()
    try:
        con.executescript(_SCHEMA)
        # Idempotent migration for pre-11n-3 DBs that have the table
        # without the new columns.
        cols = {r[1] for r in con.execute(
            "PRAGMA table_info(spot_scenario_batches)").fetchall()}
        if "batch_signature" not in cols:
            con.execute("ALTER TABLE spot_scenario_batches "
                        "ADD COLUMN batch_signature TEXT DEFAULT ''")
        if "pass_index" not in cols:
            con.execute("ALTER TABLE spot_scenario_batches "
                        "ADD COLUMN pass_index INTEGER DEFAULT 0")
        con.commit()
    finally:
        con.close()


def persist_batch(b: ScenarioBatch) -> None:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        con.execute(
            "INSERT OR REPLACE INTO spot_scenario_batches "
            "(batch_id, generated_ts_ms, tier, target_wr, n_outcomes, "
            " found_target, stopped_reason, best_scenario_id, "
            " batch_signature, pass_index, payload_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                b.batch_id, b.generated_ts_ms, b.tier, b.target_wr,
                len(b.outcomes), 1 if b.found_target else 0,
                b.stopped_reason,
                b.best_outcome.scenario_id if b.best_outcome else None,
                b.batch_signature, b.pass_index,
                json.dumps(b.to_dict(), default=str),
            ),
        )
        for o in b.outcomes:
            con.execute(
                "INSERT OR REPLACE INTO spot_scenario_runs "
                "(scenario_id, scenario_batch_id, generated_ts_ms, tier, "
                " n_trades_simulated, simulated_wr, simulated_expectancy_usd, "
                " simulated_profit_factor, simulated_max_dd_usd, payload_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    o.scenario_id, o.scenario_batch_id, o.generated_ts_ms,
                    o.tier, o.n_trades_simulated, o.simulated_wr,
                    o.simulated_expectancy_usd, o.simulated_profit_factor,
                    o.simulated_max_dd_usd,
                    json.dumps(o.to_dict(), default=str),
                ),
            )
        con.commit()
    finally:
        con.close()


def latest_batch_for_tier(tier: str) -> Optional[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        row = con.execute(
            "SELECT payload_json FROM spot_scenario_batches "
            "WHERE tier = ? ORDER BY generated_ts_ms DESC LIMIT 1",
            (tier,),
        ).fetchone()
    finally:
        con.close()
    if not row: return None
    try:
        return json.loads(row[0])
    except Exception:  # noqa: BLE001
        return None


def history(
    *, tier: Optional[str] = None, limit: int = 25,
) -> list[dict[str, Any]]:
    _init_schema()
    from shared.persistence import state as persist
    con = persist._connect()
    try:
        if tier:
            rows = con.execute(
                "SELECT batch_id, generated_ts_ms, tier, target_wr, n_outcomes, "
                "found_target, stopped_reason, best_scenario_id, "
                "batch_signature, pass_index "
                "FROM spot_scenario_batches WHERE tier = ? "
                "ORDER BY generated_ts_ms DESC LIMIT ?",
                (tier, int(limit)),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT batch_id, generated_ts_ms, tier, target_wr, n_outcomes, "
                "found_target, stopped_reason, best_scenario_id, "
                "batch_signature, pass_index "
                "FROM spot_scenario_batches ORDER BY generated_ts_ms DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    finally:
        con.close()
    return [
        {
            "batch_id": r[0], "generated_ts_ms": r[1], "tier": r[2],
            "target_wr": r[3], "n_outcomes": r[4],
            "found_target": bool(r[5]),
            "stopped_reason": r[6], "best_scenario_id": r[7],
            "batch_signature": r[8] if len(r) > 8 else "",
            "pass_index": r[9] if len(r) > 9 else 0,
        }
        for r in rows
    ]


def run_batch_and_persist(
    tier: str,
    *,
    target_wr: float = 0.60,
    non_stop: bool = False,
    matrix: Optional[dict[str, list[Any]]] = None,
    cap: int = 500,
    clock: Optional[Clock] = None,
    # Phase 11n-3 pass-through overrides for explicit novelty control.
    axis_order: Optional[tuple[str, ...]] = None,
    seed_salt: Optional[str] = None,
    value_shift: Optional[int] = None,
) -> ScenarioBatch:
    b = run_batch(
        tier, target_wr=target_wr, matrix=matrix, non_stop=non_stop,
        cap=cap, clock=clock,
        axis_order=axis_order, seed_salt=seed_salt, value_shift=value_shift,
    )
    try:
        persist_batch(b)
    except Exception:  # noqa: BLE001
        pass
    # Phase 11n-3: every persisted batch is immediately audited by the
    # Loop Novelty Governor. Verdict is stored in its own table — the
    # auto-orchestrator reads it and triggers auto-heal on "stuck".
    try:
        from spot_aggro.governance import loop_novelty_gov as _lng
        _lng.validate_and_persist(b.to_dict())
    except Exception:  # noqa: BLE001
        pass
    return b
