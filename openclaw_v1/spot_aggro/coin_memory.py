"""
SPOT AGGRO adaptive-universe memory.

Closes the loop between realized trade outcomes and future universe
selection. Per the design contract:

  Layer 1 — soft penalty    : win-rate multiplier in composite
                              (bounded ±10%, confidence-weighted)
  Layer 2 — cooldown        : pause a (symbol, tier, regime) bucket
                              after ≥3 consecutive losses; exponential
                              backoff, max 6h
  Layer 3 — suppression     : last-resort 24h skip on stable negative
                              evidence (trades≥15, wr<15%, sum_pnl<-1)

Granularity is (symbol, tier, regime) — never symbol-only. A coin that
fails in tier A@squeeze can still trade in tier B@trend.

All three layers are additive and controllable via env vars:
    SPOT_AGGRO_COIN_MEMORY_ENABLED        (master, default on)
    SPOT_AGGRO_COIN_MEMORY_SOFT           (layer 1, default on)
    SPOT_AGGRO_COIN_MEMORY_COOLDOWN       (layer 2, default on)
    SPOT_AGGRO_COIN_MEMORY_SUPPRESS       (layer 3, default on)

SPOT AGGRO owned module. No apex_omega imports; no perp-engine dependencies.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from shared.persistence import state as persist

log = logging.getLogger("spot_aggro.coin_memory")

# Layer-1 tunables (soft penalty)
MIN_SAMPLE = 10              # trades required before wr_adj engages
SAMPLE_CONFIDENCE_FULL = 30  # trades to reach full confidence scaling
SOFT_MAX_NUDGE = 0.10        # ±10% cap on composite multiplier

# Layer-2 tunables (cooldown)
COOLDOWN_MIN_STREAK = 3      # consecutive losses to trigger cooldown
COOLDOWN_BASE_S = 15 * 60    # 15 min baseline
COOLDOWN_MAX_S = 6 * 3600    # 6 h cap

# Layer-3 tunables (hard suppression)
SUPPRESS_MIN_TRADES = 15
SUPPRESS_MAX_WR = 0.15
SUPPRESS_MIN_LOSS_USD = -1.0
SUPPRESS_DURATION_S = 24 * 3600


def _flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "on")


def enabled() -> bool:
    return _flag("SPOT_AGGRO_COIN_MEMORY_ENABLED", True)


@dataclass
class CoinMemoryRow:
    symbol: str
    tier: str
    regime: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    sum_pnl_usd: float = 0.0
    last_exit_ts: int = 0
    loss_streak: int = 0
    suppressed_until_ts: int = 0
    updated_ts: int = 0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades > 0 else 0.0


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _fetch(symbol: str, tier: str, regime: str) -> Optional[CoinMemoryRow]:
    con = persist._connect()
    try:
        row = con.execute(
            "SELECT symbol, tier, regime, trades, wins, losses, sum_pnl_usd, "
            "       last_exit_ts, loss_streak, suppressed_until_ts, updated_ts "
            "FROM spot_aggro_coin_memory "
            "WHERE symbol = ? AND tier = ? AND regime = ?",
            (symbol, tier, regime),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    return CoinMemoryRow(**dict(row))


def record_exit(
    symbol: str,
    tier: str,
    regime: str,
    pnl_usd: float,
) -> None:
    """UPSERT the (symbol, tier, regime) bucket with one exit outcome."""
    if not enabled():
        return
    if not symbol or not tier or not regime:
        return
    now_s = int(time.time())
    is_win = pnl_usd > 0
    is_loss = pnl_usd <= 0
    con = persist._connect()
    try:
        row = con.execute(
            "SELECT trades, wins, losses, sum_pnl_usd, loss_streak, suppressed_until_ts "
            "FROM spot_aggro_coin_memory WHERE symbol = ? AND tier = ? AND regime = ?",
            (symbol, tier, regime),
        ).fetchone()
        if row is None:
            trades = 1
            wins = 1 if is_win else 0
            losses = 1 if is_loss else 0
            sum_pnl = float(pnl_usd)
            loss_streak = 1 if is_loss else 0
            suppressed_until = 0
        else:
            trades = row["trades"] + 1
            wins = row["wins"] + (1 if is_win else 0)
            losses = row["losses"] + (1 if is_loss else 0)
            sum_pnl = float(row["sum_pnl_usd"]) + float(pnl_usd)
            loss_streak = 0 if is_win else row["loss_streak"] + 1
            suppressed_until = row["suppressed_until_ts"]

        # Layer 3: evaluate last-resort suppression on this exit.
        if _flag("SPOT_AGGRO_COIN_MEMORY_SUPPRESS", True):
            wr = (wins / trades) if trades > 0 else 0.0
            if (trades >= SUPPRESS_MIN_TRADES and
                wr < SUPPRESS_MAX_WR and
                sum_pnl < SUPPRESS_MIN_LOSS_USD):
                suppressed_until = now_s + SUPPRESS_DURATION_S
                log.warning(
                    "coin_memory suppressed %s@%s@%s for 24h "
                    "(trades=%d wr=%.2f sum_pnl=%.2f)",
                    symbol, tier, regime, trades, wr, sum_pnl,
                )

        con.execute(
            "INSERT INTO spot_aggro_coin_memory "
            "(symbol, tier, regime, trades, wins, losses, sum_pnl_usd, "
            " last_exit_ts, loss_streak, suppressed_until_ts, updated_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol, tier, regime) DO UPDATE SET "
            " trades=excluded.trades, wins=excluded.wins, losses=excluded.losses,"
            " sum_pnl_usd=excluded.sum_pnl_usd, last_exit_ts=excluded.last_exit_ts,"
            " loss_streak=excluded.loss_streak,"
            " suppressed_until_ts=excluded.suppressed_until_ts,"
            " updated_ts=excluded.updated_ts",
            (symbol, tier, regime, trades, wins, losses, sum_pnl,
             now_s, loss_streak, suppressed_until, now_s),
        )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Layer 1 — soft penalty (composite multiplier)
# ---------------------------------------------------------------------------

def composite_multiplier(symbol: str, tier: str, regime: str) -> float:
    """Return a multiplier in [1 - SOFT_MAX_NUDGE, 1 + SOFT_MAX_NUDGE].

    Neutral 1.0 when:
      - feature disabled
      - no bucket exists
      - trades < MIN_SAMPLE (not enough evidence)

    Confidence scales linearly from MIN_SAMPLE to SAMPLE_CONFIDENCE_FULL.
    Never fully masks a forward signal — caps at ±10%.
    """
    if not enabled():
        return 1.0
    if not _flag("SPOT_AGGRO_COIN_MEMORY_SOFT", True):
        return 1.0
    row = _fetch(symbol, tier, regime)
    if row is None or row.trades < MIN_SAMPLE:
        return 1.0
    wr = row.win_rate
    # wr in [0, 1]; recenter around 0.5 so 50% WR → neutral.
    wr_signal = (wr - 0.5) * 2.0                  # [-1, 1]
    confidence = min(1.0, row.trades / SAMPLE_CONFIDENCE_FULL)
    nudge = wr_signal * confidence * SOFT_MAX_NUDGE
    return max(1.0 - SOFT_MAX_NUDGE, min(1.0 + SOFT_MAX_NUDGE, 1.0 + nudge))


# ---------------------------------------------------------------------------
# Layer 2 + Layer 3 — cooldown and suppression check
# ---------------------------------------------------------------------------

def _cooldown_remaining_s(row: CoinMemoryRow, now_s: int) -> int:
    if row.loss_streak < COOLDOWN_MIN_STREAK:
        return 0
    # Exponential: 15m → 30m → 1h → 2h → 4h → 6h cap
    extra = row.loss_streak - COOLDOWN_MIN_STREAK
    dur = COOLDOWN_BASE_S * (2 ** extra)
    dur = min(dur, COOLDOWN_MAX_S)
    remaining = (row.last_exit_ts + dur) - now_s
    return max(0, remaining)


def should_skip(symbol: str, tier: str, regime: str) -> tuple[bool, str]:
    """Return (skip?, reason). Callers drop the coin from this tier@regime.

    Suppression is checked first (harder signal), then cooldown.
    """
    if not enabled():
        return False, ""
    row = _fetch(symbol, tier, regime)
    if row is None:
        return False, ""
    now_s = int(time.time())

    # Layer 3: last-resort suppression (auto-decays).
    if _flag("SPOT_AGGRO_COIN_MEMORY_SUPPRESS", True):
        if row.suppressed_until_ts > now_s:
            remaining_h = (row.suppressed_until_ts - now_s) / 3600
            return True, (
                f"suppressed {remaining_h:.1f}h "
                f"(trades={row.trades} wr={row.win_rate:.2f} "
                f"pnl={row.sum_pnl_usd:.2f})"
            )

    # Layer 2: loss-streak cooldown.
    if _flag("SPOT_AGGRO_COIN_MEMORY_COOLDOWN", True):
        cooldown_left = _cooldown_remaining_s(row, now_s)
        if cooldown_left > 0:
            return True, (
                f"cooldown {cooldown_left // 60}m left "
                f"(streak={row.loss_streak})"
            )

    return False, ""


# ---------------------------------------------------------------------------
# Read-only helpers for dashboard / admin
# ---------------------------------------------------------------------------

def list_buckets(min_trades: int = 1, limit: int = 200) -> list[dict]:
    con = persist._connect()
    try:
        rows = con.execute(
            "SELECT * FROM spot_aggro_coin_memory "
            "WHERE trades >= ? "
            "ORDER BY sum_pnl_usd ASC LIMIT ?",
            (min_trades, limit),
        ).fetchall()
    finally:
        con.close()
    now_s = int(time.time())
    out = []
    for r in rows:
        d = dict(r)
        d["win_rate"] = (d["wins"] / d["trades"]) if d["trades"] else 0.0
        d["suppressed"] = d["suppressed_until_ts"] > now_s
        d["suppressed_remaining_h"] = (
            max(0, d["suppressed_until_ts"] - now_s) / 3600
            if d["suppressed"] else 0
        )
        # Synthesize cooldown_remaining_s from the row.
        row_obj = CoinMemoryRow(**{k: d[k] for k in (
            "symbol","tier","regime","trades","wins","losses","sum_pnl_usd",
            "last_exit_ts","loss_streak","suppressed_until_ts","updated_ts")})
        d["cooldown_remaining_s"] = _cooldown_remaining_s(row_obj, now_s)
        out.append(d)
    return out


def clear_bucket(symbol: str, tier: str, regime: str) -> bool:
    """Admin escape hatch: delete a single (symbol, tier, regime) row."""
    con = persist._connect()
    try:
        cur = con.execute(
            "DELETE FROM spot_aggro_coin_memory "
            "WHERE symbol = ? AND tier = ? AND regime = ?",
            (symbol, tier, regime),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()
