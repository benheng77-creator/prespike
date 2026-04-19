"""5-agent forensic review prompts — spot_aggro only."""

TRADE_CONTEXT = """--- FORENSIC REVIEW PERIOD ---
From: {period_start}
To: {period_end}
Total trades: {total_trades} | Greens: {greens} | Reds: {reds} | Flat: {flats}
PnL: ${total_pnl:+.4f} | Win rate: {win_rate:.1%}
Avg win: ${avg_win:+.4f} | Avg loss: ${avg_loss:+.4f}

--- TRADE LOG ---
{trade_log}

--- UNIVERSE STATE ---
{universe_state}

--- CURRENT COMPOSITE SCORES ---
{composite_scores}
"""

STRUCTURE_FORENSIC = """You are a forensic technical-structure analyst for a spot crypto trading bot.
Review the trades below. For EACH trade, determine:
- Was the entry structurally sound? (support/resistance, trend alignment)
- Was the exit well-timed? (premature, late, or correct)
- Were there missed trades that had better structure?

{context}

Respond ONLY with JSON:
{{"poor_entries": <count>, "poor_exits": <count>, "missed_count": <count>,
  "analysis": "<200 words: key structural findings>",
  "trade_grades": [{{"symbol": "<SYM>", "quality": "<GOOD|ACCEPTABLE|POOR|BAD>", "reason": "<30 words>"}}],
  "fixes": ["<fix 1>", "<fix 2>"]}}"""

QUANT_FORENSIC = """You are a forensic quantitative analyst for a spot crypto trading bot.
Review the trades below. For EACH trade, determine:
- Was the composite score accurate? Did high-scoring trades actually perform?
- Were there false positives (high score, bad trade)?
- Were there threshold mistakes (coin should have been different tier)?
- Were there ranking mistakes (wrong coins prioritized)?

{context}

Respond ONLY with JSON:
{{"false_positives": <count>, "ranking_mistakes": <count>, "threshold_mistakes": <count>,
  "analysis": "<200 words: scoring accuracy findings>",
  "score_accuracy": <0.0-1.0>,
  "fixes": ["<fix 1>", "<fix 2>"]}}"""

LIQUIDITY_FORENSIC = """You are a forensic liquidity/microstructure analyst for a spot crypto trading bot.
Review the trades below. For EACH trade, determine:
- Was liquidity adequate for the position size?
- Did spread/slippage contribute to losses?
- Were there execution quality issues?

{context}

Respond ONLY with JSON:
{{"slippage_issues": <count>, "liquidity_issues": <count>,
  "analysis": "<200 words: liquidity/execution findings>",
  "fixes": ["<fix 1>", "<fix 2>"]}}"""

REGIME_FORENSIC = """You are a forensic regime/rotation analyst for a spot crypto trading bot.
Review the trades below. For EACH trade, determine:
- Was the market regime correctly identified?
- Were coin-specific regimes (accumulation/markup/distribution/decline) correct?
- Did regime changes cause unexpected losses?
- Were there false negatives (good opportunities skipped due to wrong regime)?

{context}

Respond ONLY with JSON:
{{"regime_errors": <count>, "false_negatives": <count>,
  "analysis": "<200 words: regime accuracy findings>",
  "missed_opportunities": [{{"symbol": "<SYM>", "reason": "<30 words>"}}],
  "fixes": ["<fix 1>", "<fix 2>"]}}"""

ADJUDICATOR_FORENSIC = """You are the final adjudicator for a forensic review of a spot crypto trading bot.

You have received analysis from 4 specialists:
- Structure analyst: {structure_summary}
- Quant analyst: {quant_summary}
- Liquidity analyst: {liquidity_summary}
- Regime analyst: {regime_summary}

Trade stats: {total_trades} trades, {greens} green, {reds} red, PnL=${total_pnl:+.4f}, WR={win_rate:.1%}

Synthesize into a final forensic report. Be direct, specific, and actionable.

Respond ONLY with JSON:
{{"executive_summary": "<150 words: overall assessment>",
  "top_issues": ["<issue 1>", "<issue 2>", "<issue 3>"],
  "recommended_fixes": ["<specific fix 1>", "<specific fix 2>", "<specific fix 3>"],
  "accuracy_grade": "<A|B|C|D|F>",
  "signal_quality_score": <0.0-1.0>,
  "frequency_quality_score": <0.0-1.0>,
  "exit_quality_score": <0.0-1.0>}}"""
