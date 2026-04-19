"""Forensic report data models — spot_aggro only."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TradeReview:
    """Per-trade forensic analysis."""
    symbol: str
    action: str               # enter/exit
    tier: str
    side: str
    notional_usd: float
    entry_ts: float
    exit_ts: Optional[float]
    pnl_usd: float
    ret_pct: float
    verdict: str              # GREEN / RED / FLAT
    entry_composite: float
    exit_reason: str
    # LLM analysis
    quality: str              # GOOD / ACCEPTABLE / POOR / BAD
    root_cause: str           # why it won or lost
    fix: str                  # what to change
    was_false_positive: bool
    was_false_negative: bool  # should have entered but didn't (for missed)


@dataclass
class ForensicReport:
    """Full forensic report for one review period."""
    report_id: str
    generated_at: float
    period_start: float
    period_end: float
    # Summary stats
    total_trades: int = 0
    greens: int = 0
    reds: int = 0
    flats: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    expectancy: float = 0.0
    # Forensic counts
    false_positives: int = 0
    false_negatives: int = 0    # missed opportunities
    ranking_mistakes: int = 0
    threshold_mistakes: int = 0
    poor_entries: int = 0
    poor_exits: int = 0
    # Per-trade reviews
    trade_reviews: List[TradeReview] = field(default_factory=list)
    missed_opportunities: List[Dict[str, Any]] = field(default_factory=list)
    # LLM adjudicator summary
    executive_summary: str = ""
    top_issues: List[str] = field(default_factory=list)
    recommended_fixes: List[str] = field(default_factory=list)
    # Per-agent analysis
    structure_analysis: str = ""
    quant_analysis: str = ""
    liquidity_analysis: str = ""
    regime_analysis: str = ""
    adjudicator_synthesis: str = ""
    # Meta
    cost_usd: float = 0.0
    agents_ok: int = 0
    pdf_path: Optional[str] = None
    html_path: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "generated_at": self.generated_at,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "total_trades": self.total_trades,
            "greens": self.greens, "reds": self.reds, "flats": self.flats,
            "total_pnl": round(self.total_pnl, 4),
            "win_rate": round(self.win_rate, 4),
            "expectancy": round(self.expectancy, 4),
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "ranking_mistakes": self.ranking_mistakes,
            "threshold_mistakes": self.threshold_mistakes,
            "poor_entries": self.poor_entries,
            "poor_exits": self.poor_exits,
            "executive_summary": self.executive_summary[:500],
            "top_issues": self.top_issues[:5],
            "recommended_fixes": self.recommended_fixes[:5],
            "cost_usd": round(self.cost_usd, 4),
            "pdf_path": self.pdf_path,
            "trade_count": len(self.trade_reviews),
        }
