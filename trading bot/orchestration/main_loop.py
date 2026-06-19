"""Regime-aware main orchestration loop.

This module wires all layers together for the live paper-trading mode.
It wraps the existing bot pipeline (congressional signals via scheduler.py)
with regime detection, regime-aware allocation, and the risk manager.

Startup sequence:
1. Load config and validate
2. Initialize structured logger
3. Load market data and fit/load regime model
4. Initialize risk manager
5. Initialize paper broker (Alpaca or Simulated)
6. Initialize portfolio
7. Start APScheduler pipeline

Bar-by-bar (daily) additions vs. the existing bot:
- Fetch today's market bar (SPY/VIX)
- Update regime model with today's data
- Classify current regime + confidence
- Apply stability filter
- Scale all signal position sizes through AllocationEngine
- Pass scaled orders through RiskManager veto
- Log regime state to DB
- Update dashboard data store
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import yfinance as yf
import exchange_calendars as xcals
from apscheduler.schedulers.blocking import BlockingScheduler

# Existing bot modules (unchanged)
from bot.analytics import log_weekly_report
from bot.researcher import gather_research, gather_research_batch
from bot.scraper import run_scraper
from bot.signal_engine import (
    filter_disclosures, get_sector_for_ticker, compute_lag_days, get_cluster_count,
    clear_sector_cache, get_etf_close_history, clear_etf_cache,
)
from bot.committee import get_committees_for_politician
from bot.ai_analyst import score_entry_with_debate, review_exit, EntryScore, score_technical
from bot.db import get_open_positions, insert_signal, log_regime, get_nav_history, mark_take_profit_taken
from bot.universe import refresh_universe, get_universe
from bot.portfolio import Portfolio

# New regime-aware modules
from utils.event_calendar import has_upcoming_event
from system.config import Settings, settings as _default_settings
from market_data.market_feed import get_regime_data
from features.feature_pipeline import FeatureConfig
from regime.hmm_engine import HMMRegimeEngine, RegimeState
from regime.allocation_engine import AllocationEngine
from risk.risk_manager import RiskManager, RiskState
from risk.position_sizing import vol_target_size_pct, apply_conviction_tilt, atr_pct_from_ohlc, structure_stop_size_pct
from screener.factor_scorer import run_factor_screen, prefetch_screener_data, FactorCandidate
from monitoring.logger import EventType, emit_event, setup_logging
from dashboard.data_store import DashboardStore
from hedge.hedge_engine import HedgeEngine
from risk.correlation import CorrelationFilter
from technical.indicators import compute_snapshot
from technical.sector_map import SECTOR_ETF_MAP

log = logging.getLogger(__name__)
_AMS = ZoneInfo("Europe/Amsterdam")
_NYSE = xcals.get_calendar("XNYS")
_SCREENER_TOP_N = 12

# Congressional signals are supplementary. They boost conviction when combined with a
# strong factor score ("both" type), but pure congressional-only entries are capped in
# size and frequency so the portfolio isn't overweight on a single signal source.
_CONGRESSIONAL_MAX_PCT = 3.0      # max position size for congressional-only signals (% NAV)
_CONGRESSIONAL_MAX_PER_DAY = 1    # at most 1 pure congressional-only entry per morning
_ATR_FALLBACK_PCT = 10.0          # conservative (high-vol) ATR fallback when history() fails —
                                   # a smaller position results, the safe direction when
                                   # volatility is unknown (vol_target_size_pct: size ∝ 1/atr_pct)
_MIN_ECONOMIC_POSITION_PCT = 0.1  # below this, an order is not worth the commission/slippage


def _ma_conviction_delta(close_prices: np.ndarray, current_price: float) -> int:
    """Conviction adjustment (-2 to +1) based on 50- and 200-day simple moving averages.

    +1  price > MA50 > MA200  — golden cross, confirmed uptrend
     0  price > MA200, but MA50 ≤ MA200  — recovery not yet confirmed
    -1  price ≤ MA50 and price > MA200  — short-term weakness
    -2  price ≤ MA200  — structurally bearish
     0  fewer than 200 bars of history — skip, no penalty
    """
    if len(close_prices) < 200:
        return 0
    ma50 = float(np.mean(close_prices[-50:]))
    ma200 = float(np.mean(close_prices[-200:]))
    if current_price <= ma200:
        return -2
    if current_price <= ma50:
        return -1
    # price above both MAs
    return +1 if ma50 > ma200 else 0


def _get_sector_etf_close(sector: str) -> pd.Series | None:
    etf = SECTOR_ETF_MAP.get(sector)
    if etf is None:
        return None
    try:
        return get_etf_close_history(etf)
    except Exception:
        return None


class RegimeAwareOrchestrator:
    """Wraps the existing bot pipeline with regime detection and risk management."""

    def __init__(self, cfg: Settings | None = None) -> None:
        self._cfg = cfg or _default_settings
        self._cfg.validate()

        # Regime engine
        self._engine = HMMRegimeEngine(self._cfg.regime)
        self._feature_cfg = FeatureConfig(
            vol_window=self._cfg.features.vol_window,
            trend_window=self._cfg.features.trend_window,
            momentum_window=self._cfg.features.momentum_window,
            use_vix=self._cfg.features.use_vix,
            use_momentum=self._cfg.features.use_momentum,
            use_drawdown=self._cfg.features.use_drawdown,
            min_history_bars=self._cfg.features.min_history_bars,
        )

        # Allocation + risk
        self._alloc = AllocationEngine(self._cfg)
        self._risk = RiskManager(self._cfg)

        # Dashboard state
        self._store = DashboardStore(self._cfg.dashboard.data_store_path)

        # Current regime state (updated daily)
        self._regime_state: RegimeState | None = None
        self._market_data = None   # loaded on startup
        self._last_refit_date: date | None = None

        # Hedge engine
        self._hedge_engine = HedgeEngine(self._cfg)
        self._corr_filter = CorrelationFilter(self._cfg)

        # Pre-fetched screener data (populated at 13:00, consumed at 14:00 morning pipeline)
        self._screener_prefetch: dict | None = None

        # Portfolio vol gate multiplier (updated each morning; scales new entry sizes down
        # when realized portfolio vol exceeds SizingConfig.target_portfolio_vol_pct)
        self._port_vol_mult: float = 1.0

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def initialize(self, broker) -> None:
        """Load data, fit/load model, initialize portfolio. Call before start()."""
        setup_logging(self._cfg.log_level)
        emit_event(log, EventType.STARTUP, "Regime-aware orchestrator starting up")

        log.info("Loading historical market data (%s, %dy)...",
                 self._cfg.market_data.regime_ticker, self._cfg.market_data.history_years)
        self._market_data = get_regime_data(
            regime_ticker=self._cfg.market_data.regime_ticker,
            vix_ticker=self._cfg.market_data.vix_ticker,
            years=self._cfg.market_data.history_years,
        )
        log.info("Loaded %d market bars", len(self._market_data))

        # Try loading a saved model first; refit if not available
        model_path = self._cfg.regime.model_path
        if os.path.exists(model_path):
            try:
                self._engine.load(model_path)
                log.info("Loaded existing regime model from %s", model_path)
            except Exception as exc:
                log.warning("Could not load model (%s) — refitting", exc)
                self._fit_model()
        else:
            self._fit_model()

        # Initialize incremental inference state (O(K²) per bar going forward).
        # initialize_incremental() already classifies the latest bar and returns
        # its RegimeState — record it directly instead of calling _update_regime(),
        # which would re-process the same bar via update_single()/current_regime().
        regime_state = self._engine.initialize_incremental(self._market_data, self._feature_cfg)
        log.info("Incremental inference initialized")
        self._log_and_set_regime_state(regime_state)

        self._broker = broker
        self._portfolio = Portfolio(broker=broker, risk_cfg=self._cfg.risk)

        # Reconcile broker positions vs SQLite — remove any ghost positions from prior crash
        reconcile = self._portfolio.reconcile_with_broker()
        if reconcile["ghost_positions"]:
            emit_event(log, EventType.STARTUP,
                       f"Reconciled {len(reconcile['ghost_positions'])} ghost position(s): "
                       f"{reconcile['ghost_positions']}", alert=True)

        # Initialize risk manager NAV baseline
        equity = broker.get_equity() if hasattr(broker, "get_equity") else broker.get_cash()
        self._risk.start_of_day(equity)

        log.info("Orchestrator initialized. Regime: %s (conf=%.2f, stable=%s)",
                 self._regime_state.regime_label if self._regime_state else "unknown",
                 self._regime_state.confidence if self._regime_state else 0,
                 self._regime_state.is_stable if self._regime_state else False)

    def _fit_model(self) -> None:
        emit_event(log, EventType.MODEL_FIT, "Fitting HMM regime model")
        try:
            self._engine.fit(self._market_data, self._feature_cfg)
            self._engine.save(self._cfg.regime.model_path)
            emit_event(log, EventType.MODEL_FIT,
                       f"Model fitted: n={self._engine.n_regimes}, "
                       f"labels={self._engine.label_map}")
        except Exception as exc:
            emit_event(log, EventType.MODEL_FIT_FAILED, str(exc), alert=True)
            raise

    def _maybe_rolling_refit(self) -> None:
        """Refit the HMM on recent market data if the refit interval has elapsed.

        Called at the top of each morning pipeline. A failed refit leaves the
        existing model in place and emits an alert — it never crashes the loop.
        """
        interval = self._cfg.regime.refit_interval_days
        if interval <= 0 or not self._engine.is_fitted:
            return
        today = date.today()
        if self._last_refit_date is not None:
            if (today - self._last_refit_date).days < interval:
                return
        prev_label = self._regime_state.regime_label if self._regime_state else "unknown"
        try:
            emit_event(log, EventType.MODEL_FIT,
                       f"Rolling refit triggered (last={self._last_refit_date}, "
                       f"interval={interval}d, prev_regime={prev_label})")
            self._engine.rolling_refit(
                self._market_data,
                feature_cfg=self._feature_cfg,
            )
            self._last_refit_date = today
            self._engine.save(self._cfg.regime.model_path)
            regime_state = self._engine.initialize_incremental(self._market_data, self._feature_cfg)
            self._log_and_set_regime_state(regime_state)
            new_label = self._regime_state.regime_label if self._regime_state else "unknown"
            emit_event(log, EventType.MODEL_FIT,
                       f"Rolling refit complete: {prev_label} → {new_label}")
        except Exception as exc:
            emit_event(log, EventType.MODEL_FIT_FAILED,
                       f"Rolling refit failed: {exc}", alert=True)

    # ------------------------------------------------------------------
    # Daily update
    # ------------------------------------------------------------------

    def _update_market_data(self) -> None:
        """Append today's bar to the market data cache."""
        try:
            new_data = get_regime_data(
                regime_ticker=self._cfg.market_data.regime_ticker,
                vix_ticker=self._cfg.market_data.vix_ticker,
                years=1,
            )
            # Merge, keeping existing rows and adding new ones
            combined = new_data.combine_first(self._market_data)
            self._market_data = combined.sort_index()
        except Exception as exc:
            log.warning("Failed to update market data: %s", exc)

    def _log_and_set_regime_state(self, regime_state) -> None:
        """Record a freshly computed RegimeState: set self._regime_state, log
        it to the DB, and emit a REGIME_UNSTABLE alert (or info log).

        Shared by _update_regime() and initialize()/_maybe_rolling_refit(),
        which get a RegimeState directly from initialize_incremental() and
        must not re-process the same bar via _update_regime().
        """
        self._regime_state = regime_state
        today = date.today().isoformat()
        log_regime(
            date=today,
            regime_label=self._regime_state.regime_label,
            regime_index=self._regime_state.regime_index,
            confidence=self._regime_state.confidence,
            is_stable=self._regime_state.is_stable,
            n_regimes=self._regime_state.n_regimes,
        )
        if not self._regime_state.is_stable:
            emit_event(log, EventType.REGIME_UNSTABLE,
                       f"Unstable regime: {self._regime_state.regime_label} "
                       f"(conf={self._regime_state.confidence:.2f})")
        else:
            log.info("Regime: %s | conf=%.2f | stable=%s",
                     self._regime_state.regime_label,
                     self._regime_state.confidence,
                     self._regime_state.is_stable)

    def _update_regime(self) -> None:
        """Classify today's regime using the fitted model (causal, incremental)."""
        if self._market_data is None or not self._engine.is_fitted:
            return
        try:
            # Build single-bar DataFrame for today
            latest_bar = self._market_data.iloc[[-1]]  # last row

            if self._engine._last_log_alpha is not None:
                # Fast path: O(K²) incremental update
                regime_state = self._engine.update_single(
                    latest_bar, date_str=str(self._market_data.index[-1].date())
                )
            else:
                # Fallback: full classify (first run or after refit before initialize_incremental)
                regime_state = self._engine.current_regime(self._market_data, self._feature_cfg)

            self._log_and_set_regime_state(regime_state)
        except Exception as exc:
            log.warning("Regime update failed: %s — falling back to full classify", exc)
            try:
                self._regime_state = self._engine.current_regime(self._market_data, self._feature_cfg)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Pre-fetch (13:00 Amsterdam — 1 hour before morning pipeline)
    # ------------------------------------------------------------------

    def run_screener_prefetch(self) -> None:
        """Download and cache ticker fundamentals + momentum for the full universe.

        Called at 13:00 so the 14:00 morning pipeline can skip the expensive
        data-fetch step (~2-5 minutes) and go straight to scoring.
        The cache is consumed once by run_morning_pipeline, then cleared.
        """
        if not _NYSE.is_session(date.today().isoformat()):
            return
        try:
            universe = list(get_universe())
            if not universe:
                log.warning("Screener pre-fetch skipped — universe is empty")
                return
            self._screener_prefetch = prefetch_screener_data(universe)
        except Exception as exc:
            log.warning("Screener pre-fetch failed: %s — morning pipeline will fetch live", exc)
            self._screener_prefetch = None

    # ------------------------------------------------------------------
    # Morning pipeline (regime-aware)
    # ------------------------------------------------------------------

    def run_morning_pipeline(self) -> None:
        if not _NYSE.is_session(date.today().isoformat()):
            log.info("Market closed — skipping morning pipeline")
            return

        # Refresh broker position prices once per pipeline (avoids repeated yfinance calls)
        if hasattr(self._broker, "refresh_prices"):
            self._broker.refresh_prices()

        # Reconcile broker vs DB at start of each morning pipeline.
        # Clears ghost positions from crashes or manual broker interventions.
        try:
            reconcile = self._portfolio.reconcile_with_broker()
            if reconcile["ghost_positions"] or reconcile["untracked_positions"]:
                emit_event(
                    log, EventType.STARTUP,
                    f"Morning reconciliation: {len(reconcile['ghost_positions'])} ghost(s) removed, "
                    f"{len(reconcile['untracked_positions'])} untracked position(s) at broker",
                    alert=bool(reconcile["untracked_positions"]),
                )
        except Exception as exc:
            log.warning("Morning reconciliation failed: %s — continuing", exc)

        # Clear sector cache daily so stale GICS classifications don't persist across sessions
        clear_sector_cache()
        clear_etf_cache()

        self._maybe_rolling_refit()
        self._update_market_data()
        self._update_regime()

        # Stale-data kill-switch: block new entries if market data is too old
        try:
            latest_bar_date = (
                self._market_data.index[-1].date()
                if self._market_data is not None and not self._market_data.empty
                else None
            )
            if latest_bar_date is not None:
                staleness_days = (date.today() - latest_bar_date).days
                if staleness_days > self._cfg.risk.max_data_staleness_days:
                    self._risk.set_stale_data(True)
                    emit_event(log, EventType.DATA_STALE,
                               f"Market data is {staleness_days} days old — new entries blocked",
                               alert=True)
                else:
                    self._risk.set_stale_data(False)
        except Exception:
            pass

        self._update_dashboard()

        # ── Hedge regime gate ────────────────────────────────────────────
        is_hedge_now = (
            self._regime_state is not None
            and self._hedge_engine.is_hedge_regime(self._regime_state)
        )
        if not is_hedge_now:
            self._run_hedge_exits()
        # ─────────────────────────────────────────────────────────────────

        log.info("Morning pipeline started")
        self._portfolio.reset_daily_counter()
        # Long positions: existing thresholds, skip hedge positions
        self._portfolio.enforce_stop_losses(source_exclude="hedge")
        self._portfolio.enforce_take_profits(source_exclude="hedge")
        # Hedge positions: tighter stop-loss (10%), no take-profit
        if self._cfg.risk.enable_inverse_hedging:
            self._portfolio.enforce_stop_losses(
                stop_loss_pct=self._cfg.hedge.stop_loss_pct,
                source_include="hedge",
            )

        # Update risk manager with current NAV
        try:
            equity = self._broker.get_equity() if hasattr(self._broker, "get_equity") \
                else self._broker.get_cash()
            self._risk.start_of_day(equity)
            self._risk.check_circuit_breakers(equity)
        except Exception as exc:
            log.warning("Risk manager update failed: %s", exc)

        # --- Portfolio vol gate — scale down new entries if realized vol > target ---
        self._port_vol_mult = 1.0
        try:
            import pandas as pd
            _nav_series = get_nav_history()
            if len(_nav_series) >= 20:
                _realized_vol = float(
                    pd.Series(_nav_series).pct_change().dropna().std()
                    * np.sqrt(252) * 100
                )
                _target_vol = self._cfg.sizing.target_portfolio_vol_pct
                if _realized_vol > _target_vol and _target_vol > 0:
                    self._port_vol_mult = _target_vol / _realized_vol
                    log.info(
                        "Portfolio vol %.1f%% > target %.1f%% — scaling new entries by %.2f",
                        _realized_vol, _target_vol, self._port_vol_mult,
                    )
        except Exception as exc:
            log.warning("Portfolio vol gate failed — using mult=1.0: %s", exc)

        # --- Invested-pct capacity check --------------------------------
        _position_list = self._broker.get_positions()
        if _position_list:
            _nav = self._broker.get_cash() + sum(
                p["qty"] * p["current_price"] for p in _position_list
            )
            _invested_pct = (
                sum(p["qty"] * p["current_price"] for p in _position_list)
                / _nav * 100 if _nav > 0 else 0.0
            )
        else:
            _invested_pct = 0.0
        _at_capacity = _invested_pct >= self._cfg.risk.max_invested_pct
        if _at_capacity:
            log.info(
                "Portfolio at %.1f%% invested (cap %.1f%%) — skipping new entries",
                _invested_pct, self._cfg.risk.max_invested_pct,
            )

        # --- Scrape congressional disclosures (always, for DB persistence) ---
        new_disclosures = run_scraper()
        qualified = filter_disclosures(new_disclosures)
        log.info("Disclosures: %d new, %d qualified", len(new_disclosures), len(qualified))
        congress_tickers: set[str] = {disc["ticker"] for disc in qualified}

        if not _at_capacity:
            # --- Regime state as gate -----------------------------------
            if self._regime_state is None:
                log.warning("No regime state — processing signals without regime filter")

            # --- Correlation filter: pre-load holdings returns ----------
            _long_tickers = [
                pos["ticker"] for pos in get_open_positions()
                if pos.get("signal_source") != "hedge"
            ]
            self._corr_filter.load_holdings_returns(_long_tickers)
            # ------------------------------------------------------------

            sector_allocation: dict[str, float] = {}
            try:
                positions = self._broker.get_positions()
                if positions:
                    nav = self._broker.get_cash() + sum(
                        p["qty"] * p["current_price"] for p in positions
                    )
                    if nav > 0:
                        for pos in positions:
                            sector = get_sector_for_ticker(pos["ticker"])
                            pv = pos["qty"] * pos["current_price"]
                            sector_allocation[sector] = sector_allocation.get(sector, 0.0) + pv / nav * 100
            except Exception as exc:
                log.warning("Sector allocation computation failed: %s", exc)

            # Tickers already in the portfolio — updated as new positions are opened
            all_open_tickers = (
                {p["ticker"] for p in self._broker.get_positions()}
                | {pos["ticker"] for pos in get_open_positions()}
            )

            # ── Phase 1: Fundamental factor screener (primary signal source) ─────
            try:
                universe = list(get_universe())
                # Use pre-fetched data if available (populated by run_screener_prefetch at 13:00)
                prefetch = self._screener_prefetch
                self._screener_prefetch = None  # consume once
                if prefetch is None:
                    log.info("No pre-fetched screener data — fetching live (pipeline will be slower)")
                candidates = run_factor_screen(
                    universe,
                    top_n=_SCREENER_TOP_N,
                    research_workers=self._cfg.universe.research_concurrency,
                    regime_label=self._regime_state.regime_label if self._regime_state else None,
                    prefetched=prefetch,
                )
                for candidate in candidates:
                    if not self._portfolio.can_open_new_position():
                        log.info("Position limit reached — stopping Phase 1")
                        break
                    if candidate.ticker in all_open_tickers:
                        continue
                    try:
                        opened = self._process_fundamental_candidate(
                            candidate, sector_allocation, congress_tickers
                        )
                        if opened:
                            all_open_tickers.add(candidate.ticker)
                    except Exception:
                        log.exception(
                            "Failed processing fundamental candidate %s — skipping",
                            candidate.ticker,
                        )
            except Exception:
                log.exception("Phase 1 fundamental screener failed — skipping")

            # ── Phase 2: Congressional signals (supplementary) ───────────────────
            # These are capped at _CONGRESSIONAL_MAX_PCT per position and
            # _CONGRESSIONAL_MAX_PER_DAY entries per morning. When a congressional
            # ticker also appears in the fundamental screener, it was already handled
            # above with the "both" signal type and full conviction credit.
            congressional_opened = 0
            for disc in qualified:
                if not self._portfolio.can_open_new_position():
                    log.info("Position limit reached — stopping Phase 2")
                    break
                if congressional_opened >= _CONGRESSIONAL_MAX_PER_DAY:
                    log.info(
                        "Congressional daily limit (%d) reached — skipping remaining",
                        _CONGRESSIONAL_MAX_PER_DAY,
                    )
                    break
                if disc["ticker"] in all_open_tickers:
                    continue
                try:
                    opened = self._process_signal(disc, sector_allocation)
                    if opened:
                        all_open_tickers.add(disc["ticker"])
                        congressional_opened += 1
                except Exception:
                    log.exception("Failed processing %s — skipping", disc.get("ticker", "?"))

        # ── Phase 3: inverse ETF hedge pass ─────────────────────────
        if is_hedge_now:
            self._run_hedge_pass()
        # ─────────────────────────────────────────────────────────────

        self._corr_filter.clear()

    def _size_position(
        self,
        ticker: str,
        sector: str,
        entry_price: float,
        score_conviction: int,
        signal_type: str,
    ) -> tuple[float, float | None] | None:
        """ATR-based deterministic position sizing + MA50/MA200 conviction modifier,
        plus (if SizingConfig.enable_technical_gate) the technical-analysis gate.

        Shared by _process_signal and _process_fundamental_candidate so the two
        candidate-processing paths cannot silently drift apart. Returns
        (base_pct, initial_stop_pct) on success, or None if the candidate should
        be rejected.
        """
        _t = yf.Ticker(ticker)
        ma_delta = 0
        try:
            hist = _t.history(period="2y")
            atr_pct = atr_pct_from_ohlc(
                hist["High"].values, hist["Low"].values, hist["Close"].values,
                window=self._cfg.sizing.atr_window,
            )
            ma_delta = _ma_conviction_delta(hist["Close"].values, entry_price)
        except Exception:
            atr_pct = _ATR_FALLBACK_PCT
            hist = None

        conviction = max(1, min(10, score_conviction + ma_delta))

        initial_stop_pct: float | None = None
        use_vol_target = True
        if self._cfg.sizing.enable_technical_gate and hist is not None:
            try:
                snapshot = compute_snapshot(
                    ticker=ticker,
                    ohlcv=hist,
                    spy_close=get_etf_close_history("SPY"),
                    sector_close=_get_sector_etf_close(sector),
                    as_of=date.today().isoformat(),
                )
            except Exception:
                # Transient infra failure (ETF/price fetch) — degrade to vol-target
                # sizing rather than rejecting a candidate the upstream AI score
                # already approved, matching this codebase's fail-open pattern
                # (correlation filter, ADV check, ATR fallback).
                log.warning(
                    "Technical-gate snapshot failed for %s (%s) — falling back to vol-target sizing",
                    ticker, signal_type, exc_info=True,
                )
                snapshot = None

            if snapshot is not None and not snapshot.data_complete:
                emit_event(
                    log, EventType.SIGNAL_REJECTED,
                    f"{ticker} ({signal_type}) rejected by technical gate: insufficient "
                    f"price history (bars_available={snapshot.bars_available})",
                )
                return None

            tech_score = None
            if snapshot is not None:
                try:
                    tech_score = score_technical(
                        snapshot,
                        regime_label=self._regime_state.regime_label if self._regime_state else "neutral",
                        signal_type=signal_type,
                    )
                except Exception:
                    # LLM call/parse retry exhaustion — same fail-open rationale
                    # as the snapshot fetch above.
                    log.warning(
                        "score_technical failed for %s (%s) — falling back to vol-target sizing",
                        ticker, signal_type, exc_info=True,
                    )

            if tech_score is not None:
                if (tech_score.entry != "buy"
                        or tech_score.reward_risk < self._cfg.sizing.min_reward_risk):
                    emit_event(
                        log, EventType.SIGNAL_REJECTED,
                        f"{ticker} ({signal_type}) rejected by technical gate "
                        f"(entry={tech_score.entry}, reward_risk={tech_score.reward_risk:.2f})",
                    )
                    return None
                # tech_score.invalidation_price was only geometry-checked against
                # the history close used for scoring, not this live entry_price
                # fetched moments later. Re-check here — a gap-down through the
                # invalidation level breaks the structural stop (zero/negative
                # width).
                if tech_score.invalidation_price >= entry_price:
                    emit_event(
                        log, EventType.SIGNAL_REJECTED,
                        f"{ticker} ({signal_type}) rejected by technical gate: stop geometry "
                        f"invalid vs. live price (entry_price={entry_price:.2f}, "
                        f"invalidation_price={tech_score.invalidation_price:.2f})",
                    )
                    return None
                base_pct = structure_stop_size_pct(
                    entry_price=entry_price,
                    invalidation_price=tech_score.invalidation_price,
                    per_trade_risk_pct=self._cfg.sizing.per_trade_risk_pct,
                    max_position_pct=self._cfg.risk.max_position_pct,
                )
                initial_stop_pct = (entry_price - tech_score.invalidation_price) / entry_price * 100
                use_vol_target = False

        if use_vol_target:
            base_pct = vol_target_size_pct(
                atr_pct=atr_pct,
                per_trade_risk_pct=self._cfg.sizing.per_trade_risk_pct,
                max_position_pct=self._cfg.risk.max_position_pct,
            )
        base_pct = apply_conviction_tilt(
            base_pct=base_pct,
            conviction=conviction,
            max_position_pct=self._cfg.risk.max_position_pct,
        )
        log.debug(
            "%s (%s): vol-target base_pct=%.2f%% (atr_pct=%.2f%%, conviction=%d→%d ma_delta=%+d)",
            ticker, signal_type, base_pct, atr_pct, score_conviction, conviction, ma_delta,
        )
        return base_pct, initial_stop_pct

    def _process_signal(self, disc: dict, sector_allocation: dict) -> bool:
        """Process a congressional disclosure signal. Returns True if a position was opened."""
        ticker = disc["ticker"]
        committees = get_committees_for_politician(disc["politician"])
        sector = get_sector_for_ticker(ticker)
        lag = compute_lag_days(disc["transaction_date"], disc["disclosure_date"])
        since = (date.today() - timedelta(days=30)).isoformat()
        cluster_count = get_cluster_count(ticker, since)
        # Skip before expensive research call if an event is imminent
        has_event, event_reason = has_upcoming_event(
            ticker, window_days=self._cfg.universe.event_exclusion_window_days
        )
        if has_event:
            log.info("Skipping %s: upcoming event — %s", ticker, event_reason)
            return False

        research = gather_research(ticker)

        # AI entry scoring
        score: EntryScore = score_entry_with_debate(
            disc, committees=committees, sector=sector,
            lag_days=lag, estimated_cost_pct=1.5,
            research=research, cluster_count=cluster_count,
        )
        if score.entry != "buy":
            log.info("Skipping %s: AI conviction %d", ticker, score.conviction)
            return False

        # Log the LLM's position_pct for diagnostic comparison (not used for sizing)
        log.debug("%s: LLM position_pct=%.2f%% (ignored for sizing)", ticker, score.position_pct)

        # Price fetch — fast_info.last_price is available pre/post market; avoids stale close
        _t = yf.Ticker(ticker)
        try:
            entry_price = _t.fast_info.last_price or 0.0
        except Exception:
            entry_price = 0.0
        if not entry_price:
            emit_event(log, EventType.DEAD_FEED,
                       f"No price available for {ticker} — yfinance returned None",
                       alert=True)
            return False

        sized = self._size_position(
            ticker=ticker, sector=sector, entry_price=entry_price,
            score_conviction=score.conviction, signal_type="congressional",
        )
        if sized is None:
            return False
        base_pct, initial_stop_pct = sized

        # Regime allocation scaling
        if self._regime_state is not None:
            alloc_decision = self._alloc.compute(ticker, base_pct, self._regime_state)
            final_pct = alloc_decision.final_position_pct
            if final_pct < _MIN_ECONOMIC_POSITION_PCT:
                emit_event(log, EventType.SIGNAL_REJECTED,
                           f"{ticker} blocked by regime allocation ({alloc_decision.rationale})")
                return False
        else:
            final_pct = base_pct

        # Congressional-only signals are supplementary — cap position size
        final_pct = min(final_pct, _CONGRESSIONAL_MAX_PCT)

        # Correlation filter
        corr_mult = self._corr_filter.size_multiplier(ticker)
        final_pct *= corr_mult
        if final_pct < _MIN_ECONOMIC_POSITION_PCT:
            emit_event(log, EventType.SIGNAL_REJECTED,
                       f"{ticker} reduced to zero by correlation filter (mult={corr_mult:.2f})")
            return False

        # Portfolio vol gate: scale down if realized portfolio vol exceeds target
        final_pct *= self._port_vol_mult
        if final_pct < _MIN_ECONOMIC_POSITION_PCT:
            emit_event(log, EventType.SIGNAL_REJECTED,
                       f"{ticker} reduced to zero by portfolio-vol gate (mult={self._port_vol_mult:.3f})")
            return False

        _positions_now = self._broker.get_positions()
        _invested_usd = sum(p["qty"] * p["current_price"] for p in _positions_now)
        _nav = self._broker.get_cash() + _invested_usd
        _current_invested_pct = (_invested_usd / _nav * 100.0) if _nav > 0 else 0.0
        position_size_usd = _nav * final_pct / 100
        adv_usd = research.avg_daily_volume_usd if research else None
        veto = self._risk.validate_order(
            ticker=ticker, position_pct=final_pct, sector=sector,
            sector_allocation=sector_allocation,
            position_size_usd=position_size_usd, adv_usd=adv_usd,
            current_invested_pct=_current_invested_pct,
        )

        if not veto.allowed:
            emit_event(log, EventType.RISK_VETO,
                       f"{ticker} vetoed: {veto.reason}")
            return False

        # Apply size multiplier from risk state (e.g. 0.5× during daily drawdown)
        final_pct *= veto.size_multiplier
        if final_pct < _MIN_ECONOMIC_POSITION_PCT:
            emit_event(log, EventType.SIGNAL_REJECTED,
                       f"{ticker} reduced to zero by risk-state size multiplier (mult={veto.size_multiplier:.3f})")
            return False

        signal_id = insert_signal(
            disc["id"], ticker, score.conviction,
            final_pct, score.rationale, list(score.risk_flags),
        )
        self._portfolio.open_position(
            ticker=ticker, position_pct=final_pct,
            signal_id=signal_id, rationale=score.rationale, entry_price=entry_price,
            initial_stop_pct=initial_stop_pct,
        )
        sector_allocation[sector] = sector_allocation.get(sector, 0.0) + final_pct
        emit_event(log, EventType.ORDER_PLACED,
                   f"Opened {ticker} (congressional) pct={final_pct:.1f}% conv={score.conviction}",
                   data={"ticker": ticker, "pct": final_pct,
                         "regime": self._regime_state.regime_label if self._regime_state else "?",
                         "conviction": score.conviction})
        return True

    def _process_fundamental_candidate(
        self,
        candidate: FactorCandidate,
        sector_allocation: dict,
        congress_tickers: set,
    ) -> bool:
        """Score a fundamental screener candidate and open a position if approved.

        Returns True if a position was opened.
        """
        ticker = candidate.ticker
        signal_type = "both" if ticker in congress_tickers else "fundamental"
        sector = get_sector_for_ticker(ticker)

        has_event, event_reason = has_upcoming_event(
            ticker, window_days=self._cfg.universe.event_exclusion_window_days
        )
        if has_event:
            log.info("Skipping %s (%s): upcoming event — %s",
                     ticker, signal_type, event_reason)
            return False

        score: EntryScore = score_entry_with_debate(
            disclosure=None,
            committees=[],
            sector=sector,
            lag_days=0,
            estimated_cost_pct=1.5,
            research=candidate.research,
            signal_type=signal_type,
            factor_score=candidate.composite_score,
            ticker=ticker,
        )

        if score.entry != "buy":
            log.info("Skipping %s (%s): conviction %d", ticker, signal_type, score.conviction)
            return False

        # Log the LLM's position_pct for diagnostic comparison (not used for sizing)
        log.debug("%s: LLM position_pct=%.2f%% (ignored for sizing)", ticker, score.position_pct)

        # Price fetch — fast_info.last_price is available pre/post market; avoids stale close
        _t = yf.Ticker(ticker)
        try:
            entry_price = _t.fast_info.last_price or 0.0
        except Exception:
            entry_price = 0.0
        if not entry_price:
            emit_event(log, EventType.DEAD_FEED,
                       f"No price available for {ticker} — yfinance returned None",
                       alert=True)
            return False

        sized = self._size_position(
            ticker=ticker, sector=sector, entry_price=entry_price,
            score_conviction=score.conviction, signal_type=signal_type,
        )
        if sized is None:
            return False
        base_pct, initial_stop_pct = sized

        if self._regime_state is not None:
            alloc_decision = self._alloc.compute(ticker, base_pct, self._regime_state)
            final_pct = alloc_decision.final_position_pct
            if final_pct < _MIN_ECONOMIC_POSITION_PCT:
                emit_event(
                    log, EventType.SIGNAL_REJECTED,
                    f"{ticker} blocked by regime ({alloc_decision.rationale})",
                )
                return False
        else:
            final_pct = base_pct

        # Correlation filter
        corr_mult = self._corr_filter.size_multiplier(ticker)
        final_pct *= corr_mult
        if final_pct < _MIN_ECONOMIC_POSITION_PCT:
            emit_event(
                log, EventType.SIGNAL_REJECTED,
                f"{ticker} ({signal_type}) reduced to zero by correlation filter "
                f"(mult={corr_mult:.2f})",
            )
            return False

        # Portfolio vol gate: scale down if realized portfolio vol exceeds target
        final_pct *= self._port_vol_mult
        if final_pct < _MIN_ECONOMIC_POSITION_PCT:
            emit_event(
                log, EventType.SIGNAL_REJECTED,
                f"{ticker} reduced to zero by portfolio-vol gate (mult={self._port_vol_mult:.3f})",
            )
            return False

        _positions_now = self._broker.get_positions()
        _invested_usd = sum(p["qty"] * p["current_price"] for p in _positions_now)
        _nav = self._broker.get_cash() + _invested_usd
        _current_invested_pct = (_invested_usd / _nav * 100.0) if _nav > 0 else 0.0
        position_size_usd = _nav * final_pct / 100
        adv_usd = candidate.research.avg_daily_volume_usd if candidate.research else None
        veto = self._risk.validate_order(
            ticker=ticker,
            position_pct=final_pct,
            sector=sector,
            sector_allocation=sector_allocation,
            position_size_usd=position_size_usd,
            adv_usd=adv_usd,
            current_invested_pct=_current_invested_pct,
        )

        if not veto.allowed:
            emit_event(log, EventType.RISK_VETO, f"{ticker} vetoed: {veto.reason}")
            return False

        final_pct *= veto.size_multiplier
        if final_pct < _MIN_ECONOMIC_POSITION_PCT:
            emit_event(
                log, EventType.SIGNAL_REJECTED,
                f"{ticker} reduced to zero by risk-state size multiplier (mult={veto.size_multiplier:.3f})",
            )
            return False

        self._portfolio.open_position(
            ticker=ticker,
            position_pct=final_pct,
            signal_id=None,
            rationale=score.rationale,
            entry_price=entry_price,
            signal_source=signal_type,
            initial_stop_pct=initial_stop_pct,
        )
        try:
            from bot.db import insert_fundamental_signal
            insert_fundamental_signal(
                ticker=ticker,
                signal_date=date.today().isoformat(),
                composite_score=candidate.composite_score,
                position_pct=final_pct,
                rationale=score.rationale,
                signal_source=signal_type,
            )
        except Exception as exc:
            log.debug("Could not persist fundamental signal for %s: %s", ticker, exc)
        sector_allocation[sector] = sector_allocation.get(sector, 0.0) + final_pct
        emit_event(
            log, EventType.ORDER_PLACED,
            f"Opened {ticker} ({signal_type}) pct={final_pct:.1f}% conv={score.conviction}",
            data={
                "ticker": ticker, "pct": final_pct,
                "regime": self._regime_state.regime_label if self._regime_state else "?",
                "conviction": score.conviction,
                "signal_type": signal_type,
                "factor_score": candidate.composite_score,
            },
        )
        return True

    # ------------------------------------------------------------------
    # Hedge entry / exit
    # ------------------------------------------------------------------

    def _run_hedge_pass(self) -> None:
        """Open inverse ETF positions for the current hedge regime."""
        try:
            positions = self._broker.get_positions()
            cash = self._broker.get_cash()
            nav = cash + sum(p["qty"] * p["current_price"] for p in positions) if positions else cash

            # Sector allocation from long positions only (exclude hedges)
            open_positions_meta = get_open_positions()
            sector_allocation: dict[str, float] = {}
            if positions and nav > 0:
                for pos in positions:
                    meta = next(
                        (m for m in open_positions_meta if m["ticker"] == pos["ticker"]), {}
                    )
                    if meta.get("signal_source") == "hedge":
                        continue
                    sector = get_sector_for_ticker(pos["ticker"])
                    pv = pos["qty"] * pos["current_price"]
                    sector_allocation[sector] = sector_allocation.get(sector, 0.0) + pv / nav * 100

            orders = self._hedge_engine.compute_hedge_plan(
                self._regime_state,
                open_positions_meta,
                sector_allocation,
                nav,
            )
            if not orders:
                log.info("Hedge pass: no eligible ETFs for regime %s",
                         self._regime_state.regime_label if self._regime_state else "?")
                return

            for order in orders:
                try:
                    try:
                        entry_price = yf.Ticker(order.ticker).fast_info.last_price or 0.0
                    except Exception:
                        entry_price = 0.0
                    if not entry_price:
                        log.warning("No price for hedge ETF %s — skipping", order.ticker)
                        continue

                    position_size_usd = nav * order.position_pct / 100
                    # adv_usd=None: ETFs have no ADV constraint; validate_order skips
                    # liquidity check for sector="Hedge". In DELEVERAGE state,
                    # veto_new_entry blocks new hedges — that is intentional: existing
                    # hedges survive via source_exclude on _close_all_positions, and
                    # new hedges are not opened during an active deleverage event.
                    veto = self._risk.validate_order(
                        ticker=order.ticker,
                        position_pct=order.position_pct,
                        sector="Hedge",
                        sector_allocation={},
                        position_size_usd=position_size_usd,
                        adv_usd=None,
                    )
                    if not veto.allowed:
                        emit_event(log, EventType.RISK_VETO,
                                   f"Hedge {order.ticker} vetoed: {veto.reason}")
                        continue

                    effective_pct = order.position_pct * veto.size_multiplier
                    self._portfolio.open_position(
                        ticker=order.ticker,
                        position_pct=effective_pct,
                        signal_id=None,
                        rationale=order.rationale,
                        entry_price=entry_price,
                        signal_source="hedge",
                    )
                    emit_event(
                        log, EventType.HEDGE_ENTRY,
                        f"Opened hedge {order.ticker} pct={effective_pct:.1f}%",
                        data={
                            "ticker": order.ticker,
                            "position_pct": effective_pct,
                            "regime_label": self._regime_state.regime_label if self._regime_state else "?",
                            "regime_confidence": self._regime_state.confidence if self._regime_state else 0.0,
                            "rationale": order.rationale,
                        },
                        alert=True,
                    )
                except Exception:
                    log.exception("Failed to open hedge position %s", order.ticker)
        except Exception as exc:
            log.warning("Hedge pass failed: %s", exc)

    def _run_hedge_exits(self) -> None:
        """Close all open hedge positions (called when regime leaves bear/crash)."""
        try:
            open_positions_meta = get_open_positions()
            tickers = self._hedge_engine.get_exits_needed(open_positions_meta)
            if not tickers:
                return
            current_label = self._regime_state.regime_label if self._regime_state else "unknown"
            for ticker in tickers:
                try:
                    pos_meta = next(
                        (p for p in open_positions_meta if p["ticker"] == ticker), None
                    )
                    if pos_meta is None:
                        continue
                    try:
                        exit_price = yf.Ticker(ticker).fast_info.last_price or 0.0
                    except Exception:
                        exit_price = 0.0
                    if not exit_price:
                        log.warning("No price for hedge exit %s — skipping", ticker)
                        continue
                    self._portfolio.close_position(
                        ticker=ticker,
                        shares=pos_meta["shares"],
                        exit_price=exit_price,
                        exit_reason="regime_transition",
                        signal_id=None,
                        entry_price=pos_meta["entry_price"],
                        entry_date=pos_meta["entry_date"],
                        signal_source="hedge",
                    )
                    emit_event(
                        log, EventType.HEDGE_EXIT,
                        f"Closed hedge {ticker}: regime → {current_label}",
                        data={
                            "ticker": ticker,
                            "exit_reason": "regime_transition",
                            "exit_regime": current_label,
                        },
                        alert=True,
                    )
                except Exception:
                    log.exception("Failed to close hedge position %s", ticker)
        except Exception as exc:
            log.warning("Hedge exit pass failed: %s", exc)

    # ------------------------------------------------------------------
    # Exit review
    # ------------------------------------------------------------------

    def run_exit_review(self) -> None:
        if not _NYSE.is_session(date.today().isoformat()):
            return
        log.info("Exit review started")
        positions = [
            pos for pos in get_open_positions()
            if pos.get("signal_source") != "hedge"
        ]
        if not positions:
            return

        tickers = [pos["ticker"] for pos in positions]
        try:
            research_map = gather_research_batch(
                tickers,
                max_workers=self._cfg.universe.research_concurrency,
            )
        except Exception:
            log.exception("gather_research_batch failed in exit review; using empty map")
            research_map = {}

        for pos in positions:
            try:
                try:
                    current_price = yf.Ticker(pos["ticker"]).fast_info.last_price or pos["entry_price"]
                except Exception:
                    current_price = pos["entry_price"]
                days_held = (date.today() - date.fromisoformat(pos["entry_date"])).days
                research = research_map.get(pos["ticker"])
                decision = review_exit(pos["ticker"], pos["entry_price"],
                                       current_price, days_held, research=research)
                if decision.action == "exit":
                    self._portfolio.close_position(
                        pos["ticker"], pos["shares"], exit_price=current_price,
                        exit_reason="ai_exit", signal_id=pos["signal_id"] or 0,
                        entry_price=pos["entry_price"], entry_date=pos["entry_date"],
                    )
                    log.info("Closed %s: %s", pos["ticker"], decision.rationale)
                elif decision.action == "reduce":
                    self._portfolio.reduce_position(
                        pos["ticker"], pos["shares"], exit_price=current_price,
                        signal_id=pos["signal_id"] or 0, entry_price=pos["entry_price"],
                        entry_date=pos["entry_date"],
                    )
                    mark_take_profit_taken(pos["ticker"])
                    log.info("Reduced %s: %s", pos["ticker"], decision.rationale)
            except Exception:
                log.exception("Exit review failed for %s", pos.get("ticker", "?"))

    # ------------------------------------------------------------------
    # Intraday stop-loss check (US market hours)
    # ------------------------------------------------------------------

    def run_intraday_check(self) -> None:
        """Midday stop-loss and circuit breaker check (called during US market hours)."""
        if not _NYSE.is_session(date.today().isoformat()):
            return
        log.info("Intraday check started")
        try:
            # Re-fetch equity (prices have moved since open)
            equity = self._broker.get_equity() if hasattr(self._broker, "get_equity") \
                else self._broker.get_cash()
            self._risk.check_circuit_breakers(equity)

            # Check if forced deleveraging is needed
            if self._risk.state == RiskState.DELEVERAGE:
                log.warning("Intraday: DELEVERAGE state — closing all positions")
                self._close_all_positions(reason="intraday_deleverage", source_exclude="hedge")

            # Run stop-losses on live prices (long positions)
            self._portfolio.enforce_stop_losses(source_exclude="hedge")
            # Hedge positions tighter stop
            if self._cfg.risk.enable_inverse_hedging:
                self._portfolio.enforce_stop_losses(
                    stop_loss_pct=self._cfg.hedge.stop_loss_pct,
                    source_include="hedge",
                )
            log.info("Intraday check complete. Risk state: %s", self._risk.state.value)
        except Exception as exc:
            log.warning("Intraday check failed: %s", exc)

    def _close_all_positions(self, reason: str = "forced", source_exclude: str | None = None) -> None:
        """Close all open positions immediately. Used by deleverage circuit breaker.

        Args:
            reason: exit_reason string stored on the closed position record.
            source_exclude: if set, skip positions whose signal_source matches this value.
                Pass ``"hedge"`` during forced deleveraging so inverse-ETF hedges are
                preserved — they are paying off in the very downturn that triggered
                the circuit breaker.
        """
        open_pos = get_open_positions()
        for pos in open_pos:
            if source_exclude is not None and pos.get("signal_source") == source_exclude:
                continue
            try:
                try:
                    price = yf.Ticker(pos["ticker"]).fast_info.last_price or 0.0
                except Exception:
                    price = 0.0
                if not price:
                    continue
                self._portfolio.close_position(
                    pos["ticker"], pos["shares"], exit_price=price,
                    exit_reason=reason, signal_id=pos.get("signal_id"),
                    entry_price=pos["entry_price"], entry_date=pos["entry_date"],
                    signal_source=pos.get("signal_source", "congressional"),
                )
                log.info("Force-closed %s: %s", pos["ticker"], reason)
            except Exception:
                log.exception("Failed to force-close %s", pos.get("ticker", "?"))

    # ------------------------------------------------------------------
    # EOD
    # ------------------------------------------------------------------

    def run_eod(self) -> None:
        self._portfolio.log_snapshot()
        try:
            equity = self._broker.get_equity() if hasattr(self._broker, "get_equity") \
                else self._broker.get_cash()
            self._risk.check_circuit_breakers(equity)
            if self._risk.state == RiskState.DELEVERAGE:
                log.warning("EOD: DELEVERAGE state — closing all positions")
                self._close_all_positions(reason="eod_deleverage", source_exclude="hedge")
        except Exception as exc:
            log.warning("EOD risk check failed: %s", exc)
        self._update_dashboard()
        log.info("EOD snapshot logged. Risk state: %s", self._risk.state.value)

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    def _update_dashboard(self) -> None:
        try:
            rs = self._regime_state
            equity = self._broker.get_equity() if hasattr(self._broker, "get_equity") \
                else self._broker.get_cash()
            self._store.update({
                "regime": {
                    "label": rs.regime_label if rs else "unknown",
                    "confidence": round(rs.confidence, 3) if rs else 0.0,
                    "is_stable": rs.is_stable if rs else False,
                    "n_regimes": rs.n_regimes if rs else 0,
                    "posteriors": rs.raw_posteriors if rs else [],
                },
                "portfolio": {
                    "equity": round(equity, 2),
                    "cash": round(self._broker.get_cash(), 2),
                    "positions": self._broker.get_positions(),
                },
                "risk": self._risk.status_dict(),
            })
        except Exception as exc:
            log.warning("Dashboard update failed: %s", exc)

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------

    def start(self) -> None:
        from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
        from apscheduler.executors.pool import ThreadPoolExecutor
        from monitoring.alerts import fire_alert

        # Single-thread executor: jobs run sequentially. This prevents concurrent
        # access to the portfolio and SQLite DB when the morning pipeline overlaps
        # with the exit review. Delayed jobs are logged as missed (not silently dropped).
        executors = {"default": ThreadPoolExecutor(1)}
        scheduler = BlockingScheduler(executors=executors, timezone=_AMS)

        def _on_job_error(event) -> None:
            msg = f"Scheduler job '{event.job_id}' raised an exception: {event.exception}"
            log.error(msg)
            try:
                fire_alert("job_error", msg, {"job_id": event.job_id,
                                               "exception": str(event.exception)})
            except Exception:
                pass

        def _on_job_missed(event) -> None:
            log.warning("Scheduler job missed: %s (scheduled=%s)", event.job_id,
                        event.scheduled_run_time)

        scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)
        scheduler.add_listener(_on_job_missed, EVENT_JOB_MISSED)

        # misfire_grace_time: if a job fires late by more than this many seconds it
        # is still run (rather than silently skipped). Intraday checks get a tighter
        # window — a stop-loss check that's an hour late is not useful.
        _grace = 3600   # 1 hour for pipeline / eod (expected to be long)
        _intraday_grace = 600  # 10 min for time-sensitive intraday checks

        scheduler.add_job(refresh_universe, "cron", day_of_week="mon", hour=7, minute=0,
                          misfire_grace_time=_grace)
        # 13:00: pre-fetch screener data 1 hour before the morning pipeline
        scheduler.add_job(self.run_screener_prefetch, "cron", hour=13, minute=0,
                          misfire_grace_time=_grace)
        scheduler.add_job(self.run_morning_pipeline, "cron", hour=14, minute=0,
                          misfire_grace_time=_grace)
        scheduler.add_job(self.run_exit_review, "cron", hour=16, minute=0,
                          misfire_grace_time=_grace)
        # 15:45 = 15 mins after US open (15:30 Amsterdam = 09:30 EST)
        scheduler.add_job(self.run_intraday_check, "cron", hour=15, minute=45,
                          misfire_grace_time=_intraday_grace)
        scheduler.add_job(self.run_eod, "cron", hour=22, minute=30,
                          misfire_grace_time=_grace)
        scheduler.add_job(log_weekly_report, "cron", day_of_week="fri", hour=22, minute=45,
                          misfire_grace_time=_grace)
        # Midday / afternoon US market checks (17:00 = 11:00 EST, 20:00 = 14:00 EST)
        scheduler.add_job(self.run_intraday_check, "cron", hour=17, minute=0,
                          misfire_grace_time=_intraday_grace)
        scheduler.add_job(self.run_intraday_check, "cron", hour=20, minute=0,
                          misfire_grace_time=_intraday_grace)
        log.info("Regime-aware scheduler started (Amsterdam timezone, single-thread executor)")
        scheduler.start()
