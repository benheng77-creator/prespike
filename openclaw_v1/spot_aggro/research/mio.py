"""
Market Intelligence Object — output of each 30-min research cycle.
COPIED EXACTLY from user's APEX_OMEGA_SPOT_30MIN_RESEARCH.py spec.
NEVER MODIFY.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class MarketIntelligence:
    # From HAIKU — Regime Scanner
    regime: str = "UNKNOWN"
    regime_confidence: float = 0.5
    spi_threshold_adj: float = 0.0
    tp_multiplier: float = 1.0
    sl_multiplier: float = 1.0

    # From GPT-4o — Event Horizon
    events_next_24h: List[str] = field(default_factory=list)
    event_impact_score: float = 0.0
    event_volatility_boost: float = 1.0
    blitz_readiness: float = 0.0

    # From GEMINI — Funding Forecast
    funding_direction_24h: str = "NEGATIVE"
    funding_magnitude_pred: float = 0.0
    squeeze_timing_window: str = "NONE"
    spi_funding_weight_adj: float = 0.0

    # From DEEPSEEK — Performance Auditor
    rolling_win_rate_2h: float = 0.65
    edge_status: str = "HEALTHY"
    risk_mult_adj: float = 0.0
    recommended_position_scale: float = 1.0

    # From MISTRAL — Universe Optimizer
    top_6_assets: List[str] = field(default_factory=lambda: ['DOT', 'ADA', 'WIF', 'PEPE', 'ARB', 'SUI'])
    asset_scores: Dict[str, float] = field(default_factory=dict)
    cohort_rotation_active: bool = False
    best_cohort_pair: Optional[tuple] = None
    universe_quality: str = "NORMAL"

    # Meta
    timestamp: float = 0.0
    cycle_number: int = 0
    total_cost_usd: float = 0.0
