"""Phase 11n-9-ll — Continuous Formula Review brainstorm loop.

Runs every 6h. Pulls sufficiency + edge-contribution + variant horse-
race + recent trade_log. Produces a compressed 'brainstorm verdict'
with a single highest-value formula-level upgrade recommendation.

The output is persisted to `spot_formula_review_verdicts` so the daily
report can cite the 4 most-recent verdicts.

Fail-open. Never raises.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
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


def _init_schema() -> None:
    con = _connect()
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS spot_formula_review_verdicts("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts_ms INTEGER NOT NULL,"
            " verdict TEXT NOT NULL,"
            " headline TEXT NOT NULL,"
            " rationale TEXT,"
            " top_upgrade TEXT,"
            " payload_json TEXT"
            ")"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_frv_ts "
            "ON spot_formula_review_verdicts(ts_ms DESC)"
        )
    finally:
        con.close()


@dataclass
class FormulaReviewVerdict:
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    verdict: str = "insufficient_data"
    # verdict in: 'keep'|'tune'|'replace'|'insufficient_data'
    headline: str = ""
    rationale: str = ""
    top_upgrade: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run() -> FormulaReviewVerdict:
    """Run one brainstorm cycle. Idempotent; safe to call often."""
    _init_schema()
    v = FormulaReviewVerdict()

    # Inputs.
    try:
        from spot_aggro.governance.strategy_sufficiency import evaluate as suff_eval
        suff = suff_eval()
    except Exception as e:
        suff = None
        v.rationale += f"sufficiency error: {str(e)[:80]}; "
    try:
        from spot_aggro.governance.edge_contribution import analyze as edge_analyze
        edge = edge_analyze()
    except Exception as e:
        edge = None
        v.rationale += f"edge_contribution error: {str(e)[:80]}; "
    try:
        from spot_aggro.governance.three_way_shadow import current_state as tw_state
        tw = tw_state()
    except Exception as e:
        tw = None
        v.rationale += f"three_way_shadow error: {str(e)[:80]}; "

    # Decision logic.
    if suff is not None:
        if suff.recommendation == "insufficient_sample":
            v.verdict = "insufficient_data"
            v.headline = (
                f"Sample too small (n={suff.n_observed}); cannot judge "
                f"strategy vs {suff.target_pct_per_trade * 100:.1f}% "
                f"per-trade target yet."
            )
            v.top_upgrade = "collect_more_fills"
        elif suff.recommendation == "replace":
            v.verdict = "replace"
            v.headline = (
                f"STRUCTURALLY WEAK: avg_win={suff.avg_win_pct * 100:.2f}% "
                f"< target {suff.target_pct_per_trade * 100:.1f}%. "
                f"Even 100% WR won't hit target."
            )
            v.top_upgrade = (
                "replace_scorer_or_widen_TP: current scorer produces "
                "wins too small; needs either wider take-profits "
                "(2.5-4% instead of 0.7-1.3%) or a different signal "
                "regime (trend follow instead of mean-revert scalp)"
            )
        elif suff.recommendation == "tune":
            v.verdict = "tune"
            v.headline = (
                f"TUNABLE: {suff.pct_hitting_target * 100:.0f}% of last "
                f"{suff.n_observed} trades hit >=2%; needs "
                f"WR={suff.required_wr_for_target * 100:.0f}% "
                f"vs actual {suff.n_wins / max(suff.n_observed, 1) * 100:.0f}%."
            )
            # Pick upgrade based on which factor to push on.
            if edge and edge.top_negative:
                v.top_upgrade = (
                    f"remove/invert '{edge.top_negative}' from composite — "
                    f"anti-correlated with realized PnL"
                )
            elif edge and edge.top_positive:
                v.top_upgrade = (
                    f"weight '{edge.top_positive}' higher in composite — "
                    f"strongest real predictor of wins"
                )
            else:
                v.top_upgrade = "tighten filters; cut bottom-decile by composite"
        elif suff.recommendation == "keep":
            v.verdict = "keep"
            v.headline = (
                f"ON TRACK: {suff.pct_hitting_target * 100:.0f}% hit rate on "
                f"n={suff.n_observed}, Wilson-low "
                f"{suff.wilson_low * 100:.0f}%. Keep running."
            )
            v.top_upgrade = "observe_more_before_change"

    v.payload = {
        "sufficiency": suff.to_dict() if suff is not None else None,
        "edge": edge.to_dict() if edge is not None else None,
        "horse_race": tw if tw is not None else None,
    }

    # Persist.
    try:
        con = _connect()
        try:
            con.execute(
                "INSERT INTO spot_formula_review_verdicts("
                " ts_ms, verdict, headline, rationale, top_upgrade,"
                " payload_json) VALUES(?,?,?,?,?,?)",
                (v.ts_ms, v.verdict, v.headline, v.rationale,
                 v.top_upgrade, json.dumps(v.payload, default=str)),
            )
        finally:
            con.close()
    except Exception:
        pass
    return v


def latest(limit: int = 4) -> list[FormulaReviewVerdict]:
    try:
        _init_schema()
        con = _connect()
        try:
            rows = con.execute(
                "SELECT * FROM spot_formula_review_verdicts"
                " ORDER BY ts_ms DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        finally:
            con.close()
        out: list[FormulaReviewVerdict] = []
        for r in rows:
            payload = {}
            try:
                payload = json.loads(r["payload_json"] or "{}")
            except Exception:
                pass
            out.append(FormulaReviewVerdict(
                ts_ms=int(r["ts_ms"]),
                verdict=r["verdict"],
                headline=r["headline"] or "",
                rationale=r["rationale"] or "",
                top_upgrade=r["top_upgrade"] or "",
                payload=payload,
            ))
        return out
    except Exception:
        return []
