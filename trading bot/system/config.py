"""Central typed configuration for the regime-aware trading system.

All runtime parameters live here. Environment variables are loaded only for
secrets (API keys). Everything else has a typed default and can be overridden
via environment variable or by passing a Config object directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Credentials (secrets — from environment only, never committed)
# ---------------------------------------------------------------------------

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


@dataclass(frozen=True)
class Credentials:
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    alpaca_api_key: str = field(default_factory=lambda: _env("ALPACA_API_KEY"))
    alpaca_secret_key: str = field(default_factory=lambda: _env("ALPACA_SECRET_KEY"))
    alpaca_base_url: str = field(default_factory=lambda: _env("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"))
    propublica_api_key: str = field(default_factory=lambda: _env("PROPUBLICA_API_KEY"))
    fincept_scripts_path: str = field(default_factory=lambda: _env("FINCEPT_SCRIPTS_PATH", ""))


# ---------------------------------------------------------------------------
# Universe / data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UniverseConfig:
    refresh_day_of_week: str = "mon"  # weekly universe refresh
    max_lag_days: int = 45            # discard disclosures older than this
    min_trade_usd: int = 15_000       # minimum meaningful trade size
    event_exclusion_window_days: int = 2   # block new entries within N calendar days of earnings/FOMC
    research_concurrency: int = 5     # max parallel gather_research calls


@dataclass(frozen=True)
class MarketDataConfig:
    regime_ticker: str = "SPY"        # asset used for regime feature computation
    vix_ticker: str = "^VIX"
    history_years: int = 5            # years of history to pull on startup
    bar_frequency: str = "1d"         # daily bars


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FeatureConfig:
    vol_window: int = 20              # rolling volatility window (bars)
    trend_window: int = 200           # long-term trend MA window
    momentum_window: int = 63         # ~3-month momentum
    use_vix: bool = True
    use_momentum: bool = True
    use_drawdown: bool = True
    min_history_bars: int = 220       # minimum bars needed before fitting


# ---------------------------------------------------------------------------
# Regime detection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RegimeConfig:
    candidate_counts: tuple[int, ...] = (3, 4, 5, 6, 7)  # HMM n_components to try
    selection_criterion: str = "bic"  # "bic" or "aic"
    n_iter: int = 100                 # HMM EM iterations
    random_state: int = 42
    covariance_type: str = "diag"     # only "diag" is implemented; others fall back to diag with a warning
    # Stability filter
    min_stable_bars: int = 3          # regime must persist N bars before acting on it
    # Labels — assigned in order from lowest mean return to highest
    # 3 regimes: crash=0, neutral=1, bull=2
    # 4 regimes: crash=0, bear=1, bull=2, euphoria=3
    # 5 regimes: crash=0, bear=1, neutral=2, bull=3, euphoria=4
    label_maps: dict[int, list[str]] = field(default_factory=lambda: {
        3: ["crash", "neutral", "bull"],
        4: ["crash", "bear", "bull", "euphoria"],
        5: ["crash", "bear", "neutral", "bull", "euphoria"],
        6: ["crash", "bear", "neutral", "bull", "euphoria", "melt-up"],
        7: ["crash", "deep-bear", "bear", "neutral", "bull", "euphoria", "melt-up"],
    })
    model_path: str = "regime_model.joblib"  # persisted model file
    refit_interval_days: int = 30    # refit HMM every N days; 0 = disabled


# ---------------------------------------------------------------------------
# Regime-aware allocation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AllocationConfig:
    # Multiplier applied to AI conviction-based position size per regime label
    # These scale the base position_pct that Claude recommends
    regime_size_multiplier: dict[str, float] = field(default_factory=lambda: {
        "crash":     0.3,    # reduced but still trading
        "deep-bear": 0.3,
        "bear":      0.5,    # half size in downturns
        "neutral":   0.7,    # moderate
        "bull":      1.0,    # full size
        "euphoria":  0.75,   # reduce slightly — overheating risk
        "melt-up":   0.5,
    })
    # Additional scaling by regime confidence (linear interpolation)
    min_confidence_to_trade: float = 0.40  # below this, skip new entries
    confidence_scale: bool = True          # scale size by confidence linearly
    # Size multiplier applied when the regime has been switching rapidly.
    # AllocationEngine reads this from AllocationConfig (not RegimeConfig).
    instability_penalty: float = 0.5      # multiply position size by this when regime is unstable


# ---------------------------------------------------------------------------
# Inverse ETF hedging
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HedgeConfig:
    # ETF ticker → sectors it conflicts with (empty list = no sector filter)
    inverse_etf_universe: dict[str, list[str]] = field(default_factory=lambda: {
        "SH":  [],                                         # broad S&P 500
        "PSQ": ["Technology", "Communication Services"],   # Nasdaq
        "RWM": [],                                         # Russell 2000
        "SBB": [],                                         # short small-cap
        "EFZ": [],                                         # short MSCI EAFE
    })
    # Max total inverse allocation (% of NAV) by regime label
    max_inverse_pct_by_regime: dict[str, float] = field(default_factory=lambda: {
        "bear":      30.0,
        "crash":     50.0,
        "deep-bear": 50.0,
    })
    max_single_position_pct: float = 15.0   # per-ETF cap (% of NAV)
    conflict_threshold_pct: float = 10.0    # block ETF if portfolio > this % in its sectors
    stop_loss_pct: float = 10.0             # tighter than long 15%


# ---------------------------------------------------------------------------
# Correlation-aware sizing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CorrelationConfig:
    threshold: float = 0.7        # ρ at or below this → no reduction
    window_days: int = 60         # daily return lookback period
    min_overlap_days: int = 20    # skip pair if fewer shared trading days


# ---------------------------------------------------------------------------
# Risk management
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskConfig:
    # Position limits
    max_positions: int = 20
    max_positions_per_day: int = 3
    max_position_pct: float = 8.0         # % of NAV per position
    max_sector_pct: float = 30.0          # sector concentration cap
    max_adv_pct: float = 5.0              # max % of avg daily dollar volume

    # Stop-loss / take-profit
    trailing_stop_pct: float = 15.0       # trailing from peak
    take_profit_pct: float = 25.0         # reduce at +25%
    hard_exit_pct: float = 40.0           # full exit at +40%

    # Portfolio-level circuit breakers
    daily_loss_reduce_pct: float = 3.0    # cut position sizes 50% if daily loss exceeds this
    daily_loss_halt_pct: float = 4.0      # stop new entries if daily loss exceeds this
    daily_loss_deleverage_pct: float = 6.0  # close all positions if daily loss exceeds this
    weekly_loss_halt_pct: float = 8.0     # stop new entries for rest of week
    max_drawdown_lockout_pct: float = 15.0  # lock file created; manual unlock required

    lock_file_path: str = "RISK_LOCKOUT"  # path to lock file
    max_invested_pct: float = 80.0        # max % of NAV deployed in positions
    enable_inverse_hedging: bool = True   # set False to disable all inverse ETF hedging


# ---------------------------------------------------------------------------
# Paper execution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutionConfig:
    broker_mode: str = "paper"            # "paper" (Alpaca) or "simulated" (no API)
    slippage_bps: float = 5.0            # simulated slippage in basis points
    commission_per_share: float = 0.0    # commission (zero for Alpaca paper)
    initial_simulated_cash: float = 100_000.0
    fill_delay_bars: int = 0             # bars before fill (0 = immediate)


# ---------------------------------------------------------------------------
# Walk-forward backtesting
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BacktestConfig:
    train_years: float = 3.0            # training window in years
    test_months: float = 6.0            # out-of-sample window in months
    step_months: float = 3.0            # step size between windows
    slippage_bps: float = 10.0          # higher slippage in backtest (conservative)
    commission_pct: float = 0.05        # round-trip cost % per trade
    benchmark_ticker: str = "SPY"
    min_train_bars: int = 500
    initial_cash: float = 100_000.0     # starting capital for each backtest window


# ---------------------------------------------------------------------------
# Monitoring / dashboard
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MonitoringConfig:
    alert_webhook_url: str = field(
        default_factory=lambda: _env("ALERT_WEBHOOK_URL", "")
    )


@dataclass(frozen=True)
class DashboardConfig:
    data_store_path: str = "dashboard_state.json"
    refresh_interval_seconds: int = 300  # 5-minute dashboard refresh
    port: int = 8501                     # Streamlit default port


# ---------------------------------------------------------------------------
# Top-level settings object
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "trading.db"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    timezone: str = "Europe/Amsterdam"

    credentials: Credentials = field(default_factory=Credentials)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    market_data: MarketDataConfig = field(default_factory=MarketDataConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    allocation: AllocationConfig = field(default_factory=AllocationConfig)
    hedge: HedgeConfig = field(default_factory=HedgeConfig)
    correlation: CorrelationConfig = field(default_factory=CorrelationConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)

    def validate(self) -> None:
        """Raise ValueError for obviously wrong config combinations."""
        if self.risk.daily_loss_reduce_pct >= self.risk.daily_loss_halt_pct:
            raise ValueError("daily_loss_reduce_pct must be < daily_loss_halt_pct")
        if self.risk.daily_loss_halt_pct >= self.risk.daily_loss_deleverage_pct:
            raise ValueError("daily_loss_halt_pct must be < daily_loss_deleverage_pct")
        if self.risk.daily_loss_deleverage_pct >= self.risk.max_drawdown_lockout_pct:
            raise ValueError("daily_loss_deleverage_pct must be < max_drawdown_lockout_pct")
        if self.backtest.train_years <= 0 or self.backtest.test_months <= 0:
            raise ValueError("Backtest train/test windows must be positive")
        if self.allocation.min_confidence_to_trade < 0 or self.allocation.min_confidence_to_trade > 1:
            raise ValueError("min_confidence_to_trade must be in [0, 1]")
        if self.risk.max_invested_pct <= 0 or self.risk.max_invested_pct > 100:
            raise ValueError("max_invested_pct must be in (0, 100]")


# Module-level singleton — import this everywhere
settings = Settings()
