"""YAML config loader for OpenClaw."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import yaml


@dataclass
class ExchangeConfig:
    id: str = "binance"
    symbol: str = "BTC/USDT"
    timeframe: str = "1m"


@dataclass
class ScannerConfigSection:
    """Multi-symbol scanner — runs the decision engine across a universe
    and logs the current best pair. In "recommend" mode it does NOT trade
    the winner; it only reports. Switch to "auto" when you're ready to
    let it swap the trading symbol on its own (requires per-symbol
    tracker rebuild — not wired yet)."""

    enabled: bool = False
    mode: str = "recommend"  # recommend | auto
    universe: list = field(default_factory=list)
    max_concurrent: int = 1
    rank_by: str = "score_x_ev"
    min_bars_before_trade: int = 250


@dataclass
class AccuracyGateSection:
    """Rolling win-rate floor + proving period. Refuses EXECUTE when the
    recent win rate drops below `floor_pct` (default 75%), and refuses
    LIVE trading on any symbol until it has accumulated `proving_wins`
    paper wins first."""

    enabled: bool = True
    window_size: int = 30
    floor_pct: float = 0.75
    min_trades_before_floor: int = 10
    proving_wins: int = 5
    cooldown_bars: int = 48


@dataclass
class LiveTradingSection:
    """Live exchange-order rails. DRY-RUN by default — flipping
    `enabled: true` is NOT enough. See core/live_executor.py."""

    enabled: bool = False
    max_notional_quote: float = 50.0
    max_trades_per_day: int = 5
    reconcile_timeout_s: float = 10.0
    reconcile_tolerance_pct: float = 0.01
    env_flag: str = "OPENCLAW_LIVE_TRADING"


@dataclass
class OpenClawConfig:
    mode: str = "paper"
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    warmup_bars: int = 250
    starting_balance: float = 10000.0

    # Position sizing and risk
    max_leverage: float = 3.0
    max_drawdown: float = 0.05
    max_daily_loss: float = 0.03
    max_consecutive_losses: int = 5
    kelly_fraction: float = 0.5
    halt_file_path: Optional[str] = "cache/.halt"

    # Decision gate overrides
    exec_min: float = 72.0
    watch_min: float = 58.0

    # ---------- High win-rate mode ----------
    # Extra filters that trade count for quality. Default OFF.
    strict_mode: bool = False
    require_mtf_aligned: bool = False     # all 4 MTF timeframes must agree in sign
    min_abs_mtf: float = 0.0              # |MTFRaw| must be >= this
    max_drift_for_entry: float = 1.0      # veto if drift > this
    strict_exec_min: float = 80.0         # raises score threshold when strict_mode: true

    # ---------- Trade geometry ----------
    tp_atr_mult: float = 3.0
    sl_atr_mult: float = 1.5

    # ---------- Exit management ----------
    # Once trade reaches this R in profit, move stop to entry.
    # 0 = disabled.
    breakeven_at_r: float = 0.0

    # Conviction model
    conviction_model_type: str = "logreg"  # logreg | gbm

    # LLM-backed sentiment (opt-in, off by default — costs money)
    use_llm_sentiment: bool = False
    llm_provider: str = "claude"  # claude | openai
    llm_model: str = ""  # empty → provider default
    llm_sentiment_refresh_s: int = 600

    # Data refresh intervals
    sentiment_refresh_s: int = 300
    event_risk_refresh_s: int = 900
    funding_refresh_s: int = 60
    oi_refresh_s: int = 60

    # Paths
    trade_log_db: str = "logs/openclaw.db"
    pwin_coefficients_path: str = "cache/pwin_coefficients.json"
    conviction_model_path: str = "cache/conviction_model.pkl"

    # ---------- Launch upgrades (optional, default-off / default-safe) ----------
    scanner: ScannerConfigSection = field(default_factory=ScannerConfigSection)
    accuracy_gate: AccuracyGateSection = field(default_factory=AccuracyGateSection)
    live_trading: LiveTradingSection = field(default_factory=LiveTradingSection)

    def __post_init__(self) -> None:
        if self.mode not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got {self.mode!r}")
        if self.starting_balance <= 0:
            raise ValueError(
                f"starting_balance must be > 0, got {self.starting_balance}"
            )
        if not 0.0 < self.kelly_fraction <= 1.0:
            raise ValueError(
                f"kelly_fraction must be in (0, 1], got {self.kelly_fraction}"
            )
        if self.conviction_model_type not in ("logreg", "gbm"):
            raise ValueError(
                f"conviction_model_type must be 'logreg' or 'gbm', "
                f"got {self.conviction_model_type!r}"
            )
        if self.llm_provider not in ("claude", "openai"):
            raise ValueError(
                f"llm_provider must be 'claude' or 'openai', got {self.llm_provider!r}"
            )
        if self.tp_atr_mult <= 0:
            raise ValueError(f"tp_atr_mult must be > 0, got {self.tp_atr_mult}")
        if self.sl_atr_mult <= 0:
            raise ValueError(f"sl_atr_mult must be > 0, got {self.sl_atr_mult}")
        if self.breakeven_at_r < 0:
            raise ValueError(f"breakeven_at_r must be >= 0, got {self.breakeven_at_r}")
        if not 0.0 <= self.min_abs_mtf <= 1.0:
            raise ValueError(f"min_abs_mtf must be in [0, 1], got {self.min_abs_mtf}")
        if not 0.0 <= self.max_drift_for_entry <= 1.0:
            raise ValueError(
                f"max_drift_for_entry must be in [0, 1], got {self.max_drift_for_entry}"
            )
        if not 0.0 < self.max_daily_loss < 1.0:
            raise ValueError(
                f"max_daily_loss must be in (0, 1), got {self.max_daily_loss}"
            )
        if self.max_consecutive_losses < 1:
            raise ValueError(
                f"max_consecutive_losses must be >= 1, got {self.max_consecutive_losses}"
            )


def load_config(path: str) -> OpenClawConfig:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    exchange_raw = raw.pop("exchange", {}) or {}
    scanner_raw = raw.pop("scanner", None)
    accuracy_gate_raw = raw.pop("accuracy_gate", None)
    live_trading_raw = raw.pop("live_trading", None)
    exchange = ExchangeConfig(**exchange_raw)
    kwargs: dict = {"exchange": exchange}
    if scanner_raw is not None:
        kwargs["scanner"] = ScannerConfigSection(**scanner_raw)
    if accuracy_gate_raw is not None:
        kwargs["accuracy_gate"] = AccuracyGateSection(**accuracy_gate_raw)
    if live_trading_raw is not None:
        kwargs["live_trading"] = LiveTradingSection(**live_trading_raw)
    return OpenClawConfig(**kwargs, **raw)
