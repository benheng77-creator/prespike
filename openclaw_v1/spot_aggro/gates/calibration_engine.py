"""
L3 — Calibration engine (SPOT AGGRO only).

Builds the `(score_decile × tier × symbol × regime)` truth table from the
last N days of closed trades (default 30d) and publishes it to
`spot_aggro_calibration_table`. The L3 gate (`calibration_gate.py`) reads
that table; it does not recompute.

Bucket status rules (spec §L3, adapted per operator overrides):

    n_trades < MIN_TRADES_PER_BUCKET   →  INSUFFICIENT_DATA
    mean_pnl_pct <= 0                  →  INVALID
    mean_pnl_pct >  0 and violates monotonicity vs lower decile
                                       →  NON_MONOTONIC (tradeable but flagged)
    mean_pnl_pct >  0 and monotonic    →  VALID

Operator overrides applied here:
    * Tier C is included. No tier is excluded from calibration.
    * NON_MONOTONIC is a soft flag, not an immediate REJECT. It remains
      tradeable if expectancy is still positive — the gate treats it as a
      warning-tagged PASS (see calibration_gate).
    * Calibration sees EVERY closed trade across EVERY tier, including
      disabled-tier "trade_disabled" log entries if/when those land in the
      trade log (analytics always include all tiers).

Source of truth: `trade_log` rows with `action='exit'`, paired to
their preceding `action='enter'` row (same pairing used by forensic_v2).
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from shared.persistence import state as persist

from . import calibration_store as store

log = logging.getLogger("spot_aggro.gate.l3.engine")


# --- Build constants -------------------------------------------------------

N_DECILES = 10                   # 0..9
MIN_TRADES_PER_BUCKET = 20       # spec §L3
DEFAULT_WINDOW_DAYS = 30
DEFAULT_REGIME_LABEL = "UNKNOWN"


@dataclass(frozen=True)
class CalibrationBuildStats:
    trades_considered: int
    trades_used: int
    trades_skipped_missing: int
    buckets_written: int
    n_valid: int
    n_invalid: int
    n_insufficient: int
    n_non_monotonic: int
    window_days: int
    built_ts_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "trades_considered": self.trades_considered,
            "trades_used": self.trades_used,
            "trades_skipped_missing": self.trades_skipped_missing,
            "buckets_written": self.buckets_written,
            "n_valid": self.n_valid,
            "n_invalid": self.n_invalid,
            "n_insufficient": self.n_insufficient,
            "n_non_monotonic": self.n_non_monotonic,
            "window_days": self.window_days,
            "built_ts_ms": self.built_ts_ms,
        }


# ---------------------------------------------------------------------------
# Pure decile & bucket helpers
# ---------------------------------------------------------------------------

def score_to_decile(score: float, *, n_deciles: int = N_DECILES) -> int:
    """Map a [0, 1] composite score to a 0..n_deciles-1 bucket.

    Scores outside the range clamp. A score of exactly 1.0 maps to n-1.
    """
    if score is None or math.isnan(score):
        return 0
    if score <= 0:
        return 0
    if score >= 1.0:
        return n_deciles - 1
    return int(score * n_deciles)


@dataclass
class _Agg:
    n: int = 0
    wins: int = 0
    losses: int = 0
    sum_pct: float = 0.0
    sum_usd: float = 0.0
    last_ts_ms: Optional[int] = None


def _new_agg_table() -> dict[tuple[str, str, str, int], _Agg]:
    return {}


def _aggregate_trade(
    agg: dict[tuple[str, str, str, int], _Agg],
    *,
    tier: str,
    symbol: str,
    regime: str,
    score_decile: int,
    pnl_pct: float,
    pnl_usd: float,
    ts_ms: int,
) -> None:
    key = (tier, symbol, regime, score_decile)
    a = agg.get(key)
    if a is None:
        a = _Agg()
        agg[key] = a
    a.n += 1
    if pnl_pct > 0:
        a.wins += 1
    elif pnl_pct < 0:
        a.losses += 1
    a.sum_pct += pnl_pct
    a.sum_usd += pnl_usd
    if a.last_ts_ms is None or ts_ms > a.last_ts_ms:
        a.last_ts_ms = ts_ms


def _finalise(
    agg: dict[tuple[str, str, str, int], _Agg],
    *,
    min_trades: int = MIN_TRADES_PER_BUCKET,
) -> list[store.BucketRow]:
    """Apply status rules to each bucket and flag NON_MONOTONIC per
    (tier, symbol, regime) sequence by score_decile.
    """
    now_ms = int(time.time() * 1000)
    # First: compute per-bucket status without monotonicity
    initial: dict[tuple[str, str, str, int], store.BucketRow] = {}
    for key, a in agg.items():
        tier, symbol, regime, decile = key
        mean_pct = a.sum_pct / a.n if a.n else 0.0
        mean_usd = a.sum_usd / a.n if a.n else 0.0
        if a.n < min_trades:
            status = store.STATUS_INSUFFICIENT
        elif mean_pct <= 0.0:
            status = store.STATUS_INVALID
        else:
            status = store.STATUS_VALID
        initial[key] = store.BucketRow(
            tier=tier, symbol=symbol, regime=regime, score_decile=decile,
            n_trades=a.n, wins=a.wins, losses=a.losses,
            mean_pnl_pct=mean_pct, sum_pnl_pct=a.sum_pct,
            mean_pnl_usd=mean_usd,
            last_trade_ts_ms=a.last_ts_ms,
            status=status,
            updated_ts_ms=now_ms,
        )

    # Monotonicity check per (tier, symbol, regime): for each decile d that
    # has status VALID, the previous VALID decile d' < d must have
    # mean_pnl_pct <= current.mean_pnl_pct. If not → NON_MONOTONIC.
    #
    # Rationale: spec §L3 requires "bucket status = NON_MONOTONIC if
    # realized_expectancy < previous_decile". We compare against the
    # nearest-lower VALID bucket only; INSUFFICIENT/INVALID buckets don't
    # count as baselines.
    by_group: dict[tuple[str, str, str], list[int]] = {}
    for (t, s, r, d) in initial:
        by_group.setdefault((t, s, r), []).append(d)

    for group, deciles in by_group.items():
        deciles_sorted = sorted(deciles)
        last_valid_mean: Optional[float] = None
        for d in deciles_sorted:
            row = initial[(*group, d)]
            if row.status == store.STATUS_VALID:
                if last_valid_mean is not None and row.mean_pnl_pct < last_valid_mean:
                    initial[(*group, d)] = store.BucketRow(
                        **{**row.to_dict(), "status": store.STATUS_NON_MONOTONIC}
                    )
                last_valid_mean = row.mean_pnl_pct
            # INVALID / INSUFFICIENT don't reset the baseline.
    return list(initial.values())


# ---------------------------------------------------------------------------
# Trade fetch
# ---------------------------------------------------------------------------

def _load_trade_pairs(
    window_start_ms: int,
    window_end_ms: int,
) -> tuple[list[dict[str, Any]], int, int]:
    """Pull exit rows in window and match to preceding enters. Returns
    (rows, considered, skipped_missing_fields)."""
    persist.init_schema()
    con = persist._connect()
    try:
        exits = con.execute(
            """
            SELECT id, ts_ms, symbol, module, notional_usd, avg_px,
                   pnl_usd, payload_json
            FROM trade_log
            WHERE ts_ms >= ? AND ts_ms < ? AND action='exit'
            ORDER BY ts_ms ASC
            """,
            (window_start_ms, window_end_ms),
        ).fetchall()

        out: list[dict[str, Any]] = []
        considered = len(exits)
        skipped = 0
        for ex in exits:
            enter = con.execute(
                """
                SELECT id, ts_ms, avg_px, payload_json
                FROM trade_log
                WHERE symbol=? AND action='enter' AND ts_ms < ?
                ORDER BY ts_ms DESC LIMIT 1
                """,
                (ex["symbol"], ex["ts_ms"]),
            ).fetchone()
            if enter is None:
                skipped += 1
                continue

            try:
                en_payload = json.loads(enter["payload_json"]) if enter["payload_json"] else {}
                ex_payload = json.loads(ex["payload_json"]) if ex["payload_json"] else {}
            except (TypeError, ValueError):
                skipped += 1
                continue

            tier = (
                en_payload.get("tier")
                or ex_payload.get("tier")
                or _tier_from_module(ex["module"])
            )
            if not tier:
                skipped += 1
                continue

            composite = en_payload.get("composite") or en_payload.get("composite_score")
            if composite is None:
                skipped += 1
                continue

            entry_px = float(enter["avg_px"]) if enter["avg_px"] is not None else None
            exit_px = float(ex["avg_px"]) if ex["avg_px"] is not None else None
            if entry_px is None or exit_px is None or entry_px <= 0:
                skipped += 1
                continue

            pnl_pct = (exit_px - entry_px) / entry_px
            pnl_usd = float(ex["pnl_usd"] or 0.0)
            regime = (
                en_payload.get("entry_regime")
                or en_payload.get("regime")
                or DEFAULT_REGIME_LABEL
            )

            out.append({
                "tier": tier,
                "symbol": ex["symbol"],
                "regime": regime,
                "composite": float(composite),
                "pnl_pct": pnl_pct,
                "pnl_usd": pnl_usd,
                "ts_ms": int(ex["ts_ms"]),
            })
        return out, considered, skipped
    finally:
        con.close()


def _tier_from_module(module: Optional[str]) -> str:
    if not module:
        return ""
    m = module.upper()
    if "BLITZ" in m or "M3" in m:
        return "A+"
    if "SQUEEZE_A" in m:
        return "A"
    if "FLOW_B" in m:
        return "B"
    if "SCALP_C" in m:
        return "C"
    return ""


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------

def build_calibration_table(
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now_ts_ms: Optional[int] = None,
    min_trades_per_bucket: int = MIN_TRADES_PER_BUCKET,
    trades: Optional[Iterable[dict[str, Any]]] = None,
) -> CalibrationBuildStats:
    """Build and publish the calibration table.

    Normal usage: omit `trades` — the builder pulls from trade_log.
    For tests: pass `trades` as an iterable of dicts with keys
        tier, symbol, regime, composite, pnl_pct, pnl_usd, ts_ms.
    """
    now_ms = now_ts_ms if now_ts_ms is not None else int(time.time() * 1000)
    win_end = now_ms
    win_start = now_ms - window_days * 86_400 * 1000

    if trades is None:
        rows, considered, skipped = _load_trade_pairs(win_start, win_end)
    else:
        rows = list(trades)
        considered = len(rows)
        skipped = 0

    agg = _new_agg_table()
    used = 0
    for r in rows:
        try:
            decile = score_to_decile(float(r["composite"]))
            _aggregate_trade(
                agg,
                tier=str(r["tier"]),
                symbol=str(r["symbol"]),
                regime=str(r.get("regime") or DEFAULT_REGIME_LABEL),
                score_decile=decile,
                pnl_pct=float(r["pnl_pct"]),
                pnl_usd=float(r.get("pnl_usd") or 0.0),
                ts_ms=int(r.get("ts_ms") or now_ms),
            )
            used += 1
        except (KeyError, TypeError, ValueError):
            skipped += 1

    final_rows = _finalise(agg, min_trades=min_trades_per_bucket)
    store.replace_buckets(final_rows)

    n_valid = sum(1 for r in final_rows if r.status == store.STATUS_VALID)
    n_invalid = sum(1 for r in final_rows if r.status == store.STATUS_INVALID)
    n_insuf = sum(1 for r in final_rows if r.status == store.STATUS_INSUFFICIENT)
    n_nonmono = sum(1 for r in final_rows if r.status == store.STATUS_NON_MONOTONIC)

    stats = CalibrationBuildStats(
        trades_considered=considered,
        trades_used=used,
        trades_skipped_missing=skipped,
        buckets_written=len(final_rows),
        n_valid=n_valid,
        n_invalid=n_invalid,
        n_insufficient=n_insuf,
        n_non_monotonic=n_nonmono,
        window_days=window_days,
        built_ts_ms=now_ms,
    )
    log.info(
        "[L3 build] used=%d skipped=%d buckets=%d valid=%d invalid=%d insuf=%d nonmono=%d",
        used, skipped, len(final_rows), n_valid, n_invalid, n_insuf, n_nonmono,
    )
    return stats
