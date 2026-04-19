"""
30-minute research cycle prompts — 5 LLMs.
COPIED EXACTLY from user's APEX_OMEGA_SPOT_30MIN_RESEARCH.py spec.
NEVER MODIFY.
"""

RESEARCH_CONTEXT = """--- 30-MINUTE MARKET SNAPSHOT ---
Timestamp: {timestamp} UTC | Cycle #{cycle_number}

TOP ASSETS BY SPI:
{asset_table}

PORTFOLIO STATUS:
Capital: ${capital:.2f} | Open positions: {open_count} | DD from peak: {dd_pct:.2f}%
Trades last 2h: {trades_2h} | Wins: {wins_2h} | Losses: {losses_2h} | Win rate: {wr_2h:.0f}%
Net PnL last 2h: ${pnl_2h:+.2f} | Best trade: ${best_trade:+.2f} | Worst: ${worst_trade:+.2f}

FUNDING RATES (8h, all universe):
{funding_table}

RECENT TRADE LOG (last 10):
{trade_log}"""


RESEARCH_HAIKU_REGIME = """You are the regime classification engine for a crypto spot trading system. Every 30 minutes you classify the current market regime. Your classification directly adjusts the trading formula's thresholds.

{context}

CLASSIFY into exactly ONE regime:

SQUEEZE_BUILDING: spi_threshold_adj: -0.05, tp_multiplier: 1.3, sl_multiplier: 1.2
TRENDING_UP: spi_threshold_adj: +0.05, tp_multiplier: 1.5, sl_multiplier: 0.9
TRENDING_DOWN: spi_threshold_adj: +0.15, tp_multiplier: 0.7, sl_multiplier: 0.7
RANGE_BOUND: spi_threshold_adj: +0.10, tp_multiplier: 0.6, sl_multiplier: 0.8
CRISIS: spi_threshold_adj: -0.10, tp_multiplier: 2.0, sl_multiplier: 1.5
POST_SQUEEZE: spi_threshold_adj: +0.20, tp_multiplier: 0.5, sl_multiplier: 0.6
DEAD: spi_threshold_adj: +0.15, tp_multiplier: 0.4, sl_multiplier: 0.5

Respond ONLY with JSON:
{{"regime": "<regime>", "confidence": <float>, "spi_threshold_adj": <float>, "tp_multiplier": <float>, "sl_multiplier": <float>, "rationale": "<40 words>"}}"""


RESEARCH_GPT_EVENTS = """You are the event-horizon scanner for a crypto spot trading system. Every 30 minutes you identify upcoming catalysts that could create or destroy squeeze opportunities in the next 1-24 hours.

{context}

Respond ONLY with JSON:
{{"events": [{{"name": "<event>", "hours_until": <float>, "impact": <-1 to +1>, "probability": <0 to 1>}}], "event_impact_score": <float -1 to +1>, "event_volatility_boost": <float 1 to 3>, "blitz_readiness": <float 0 to 1>, "rationale": "<40 words>"}}"""


RESEARCH_GEMINI_FUNDING = """You are the funding-rate forecaster for a crypto spot trading system. Every 30 minutes you predict the direction, magnitude, and timing of funding rate changes across the trading universe.

{context}

Respond ONLY with JSON:
{{"funding_direction_24h": "<NEGATIVE|POSITIVE|NEUTRAL|FLIPPING>", "funding_magnitude_pred": <float>, "squeeze_timing_window": "<IMMINENT|NEAR|FAR|NONE>", "spi_funding_weight_adj": <float -0.10 to +0.10>, "confidence": <float>, "rationale": "<40 words>"}}"""


RESEARCH_DEEPSEEK_AUDIT = """You are the performance auditor for a crypto spot trading system. Every 30 minutes you analyse the last 2 hours of trading activity and assess whether the edge is intact, degrading, or lost.

{context}

Respond ONLY with JSON:
{{"rolling_win_rate_2h": <float>, "edge_status": "<HEALTHY|DEGRADING|LOST|RECOVERING|INSUFFICIENT_DATA>", "profit_factor": <float>, "avg_win_loss_ratio": <float>, "risk_mult_adj": <float -0.20 to +0.10>, "recommended_position_scale": <float 0.5 to 1.5>, "exclude_assets": [], "loss_pattern": "<20 words>", "rationale": "<40 words>"}}"""


RESEARCH_MISTRAL_UNIVERSE = """You are the universe optimisation engine for a crypto spot trading system. Every 30 minutes you re-rank the 16-coin watchlist and select the top 6 assets with the best squeeze opportunity.

{context}

Respond ONLY with JSON:
{{"top_6": [{{"symbol": "<SYM>", "score": <0 to 1>, "spi": <float>, "reason": "<10 words>"}}], "cohort_rotation_active": <bool>, "best_cohort_pair": ["<SYM1>", "<SYM2>"] or null, "cohort_spread_sigma": <float or null>, "universe_quality": "<RICH|NORMAL|THIN>", "rationale": "<40 words>"}}"""
