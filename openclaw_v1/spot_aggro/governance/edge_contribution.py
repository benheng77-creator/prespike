"""Phase 11n-9-ll — Edge Contribution Analysis.

For each signal component (spi, funding_z, composite, depth_usd,
spread_bp, ret_7d), compute the Spearman rank correlation with
realized per-trade net PnL %.

Interpretation:
  +0.3 to +1.0   strong positive contribution (factor predicts wins)
  +0.1 to +0.3   weak positive
  -0.1 to +0.1   noise (factor is dead weight)
  -0.3 to -0.1   weak negative (factor is anti-correlated — destroying edge)
  -1.0 to -0.3   strong negative (remove or invert the factor)

Fail-open. Read-only.

The 24h report uses this to answer: "which formula components are
creating edge vs destroying it?"
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any


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


FACTORS = (
    "spi", "funding_z", "composite", "composite_score",
    "depth_usd", "spread_bp", "ret_7d", "consensus",
)


@dataclass
class FactorContribution:
    factor: str
    n_paired: int
    spearman_rho: float
    verdict: str           # 'strong_positive'|'weak_positive'|'noise'|'weak_negative'|'strong_negative'|'insufficient'
    direction: str         # 'predicts_wins'|'predicts_losses'|'neutral'|'unknown'

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EdgeContributionReport:
    n_trades_with_payload: int = 0
    factors: list[FactorContribution] = field(default_factory=list)
    top_positive: str | None = None
    top_negative: str | None = None
    recommendation: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["factors"] = [f.to_dict() if hasattr(f, "to_dict") else dict(f)
                        for f in self.factors]
        return d


def _spearman(xs: list[float], ys: list[float]) -> float:
    """Simple Spearman rank correlation. Returns 0 on degenerate input."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return 0.0
    # Rank function with average-rank tie handling.
    def ranks(vs: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: vs[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vs[order[j + 1]] == vs[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0  # 1-indexed average rank
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx = ranks(xs)
    ry = ranks(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def _verdict_for_rho(rho: float, n: int) -> str:
    if n < 10:
        return "insufficient"
    abs_rho = abs(rho)
    if abs_rho < 0.1:
        return "noise"
    if abs_rho < 0.3:
        return "weak_negative" if rho < 0 else "weak_positive"
    return "strong_negative" if rho < 0 else "strong_positive"


def _direction_for(verdict: str, rho: float) -> str:
    if verdict == "insufficient":
        return "unknown"
    if verdict == "noise":
        return "neutral"
    return "predicts_wins" if rho > 0 else "predicts_losses"


def analyze(window_n: int = 200) -> EdgeContributionReport:
    rpt = EdgeContributionReport()
    # Pull the last `window_n` exit trades with their ENTER payloads. The
    # enter row carries the factor snapshot at entry time.
    try:
        con = _connect()
        try:
            # Match enters <-> exits by correlation_id OR by symbol+module
            # timing (fallback). Simplest: pull (exit, net_pct) pairs from
            # trade_log, then look up the most recent enter for that symbol
            # before the exit's ts_ms.
            exits = con.execute(
                "SELECT ts_ms, symbol, module, notional_usd, pnl_usd,"
                " fee_usd, payload_json FROM trade_log"
                " WHERE action='exit' AND notional_usd > 0"
                " ORDER BY ts_ms DESC LIMIT ?",
                (int(window_n),),
            ).fetchall()
            if not exits:
                rpt.reason = "no exits in trade_log"
                return rpt

            pairs: list[tuple[dict[str, Any], float]] = []
            for ex in exits:
                notional = float(ex["notional_usd"] or 0)
                if notional <= 0:
                    continue
                net = (float(ex["pnl_usd"] or 0) - float(ex["fee_usd"] or 0)) / notional
                # Find matching enter.
                enter = con.execute(
                    "SELECT payload_json FROM trade_log"
                    " WHERE action='enter' AND symbol=? AND ts_ms<?"
                    " ORDER BY ts_ms DESC LIMIT 1",
                    (ex["symbol"], int(ex["ts_ms"])),
                ).fetchone()
                if not enter:
                    continue
                try:
                    payload = json.loads(enter["payload_json"] or "{}")
                except Exception:
                    payload = {}
                if not payload:
                    continue
                pairs.append((payload, net))
        finally:
            con.close()
    except Exception as e:
        rpt.reason = f"db error: {str(e)[:120]}"
        return rpt

    rpt.n_trades_with_payload = len(pairs)
    if len(pairs) < 10:
        rpt.reason = f"n={len(pairs)} < 10; insufficient sample"
        return rpt

    # For each factor, build the (factor, net) arrays and Spearman it.
    for factor in FACTORS:
        xs: list[float] = []
        ys: list[float] = []
        for payload, net in pairs:
            v = payload.get(factor)
            if v is None:
                continue
            try:
                xs.append(float(v))
                ys.append(net)
            except (TypeError, ValueError):
                continue
        if len(xs) < 10:
            rpt.factors.append(FactorContribution(
                factor=factor, n_paired=len(xs),
                spearman_rho=0.0,
                verdict="insufficient",
                direction="unknown",
            ))
            continue
        rho = round(_spearman(xs, ys), 4)
        verdict = _verdict_for_rho(rho, len(xs))
        rpt.factors.append(FactorContribution(
            factor=factor, n_paired=len(xs),
            spearman_rho=rho,
            verdict=verdict,
            direction=_direction_for(verdict, rho),
        ))

    positives = [f for f in rpt.factors if f.verdict in ("strong_positive", "weak_positive")]
    negatives = [f for f in rpt.factors if f.verdict in ("strong_negative", "weak_negative")]
    if positives:
        positives.sort(key=lambda f: f.spearman_rho, reverse=True)
        rpt.top_positive = positives[0].factor
    if negatives:
        negatives.sort(key=lambda f: f.spearman_rho)
        rpt.top_negative = negatives[0].factor

    if rpt.top_negative and any(
        f.factor == rpt.top_negative and f.verdict == "strong_negative"
        for f in rpt.factors
    ):
        rpt.recommendation = (
            f"remove or invert factor '{rpt.top_negative}' — "
            f"it is anti-correlated with realized PnL"
        )
    elif rpt.top_positive:
        rpt.recommendation = (
            f"weight up factor '{rpt.top_positive}' in composite; "
            f"current formula under-uses the strongest signal"
        )
    else:
        rpt.recommendation = "no factor shows significant signal — replace scorer"
    rpt.reason = (
        f"analyzed {rpt.n_trades_with_payload} trade-pairs across "
        f"{len(rpt.factors)} factors"
    )
    return rpt
