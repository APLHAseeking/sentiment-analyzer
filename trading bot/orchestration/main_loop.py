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

import yfinance as yf
import exchange_calendars as xcals
from apscheduler.schedulers.blocking import BlockingScheduler

# Existing bot modules (unchanged)
from bot.analytics import log_weekly_report
from bot.researcher import gather_research
from bot.scraper import run_scraper
from bot.signal_engine import filter_disclosures, get_sector_for_ticker, compute_lag_days, get_cluster_count
from bot.committee import get_committees_for_politician
from bot.ai_analyst import score_entry_with_debate, review_exit, EntryScore
from bot.db import get_open_positions, insert_signal, log_regime
from bot.universe import refresh_universe, get_universe
from bot.portfolio import Portfolio

# New regime-aware modules
from utils.event_calendar import has_upcoming_event
from system.config import Settings, settings as _default_settings
from market_data.market_feed import get_regime_data
from features.feature_pipeline import FeatureConfig
from regime.hmm_engine import HMMRegimeEngine, RegimeState
from regime.allocation_engine import AllocationEngine
from risk.risk_manager import RiskManager
from screener.factor_scorer import run_factor_screen, FactorCandidate
from monitoring.logger import EventType, emit_event, setup_logging
from dashboard.data_store import DashboardStore
from hedge.hedge_engine import HedgeEngine

log = logging.getLogger(__name__)
_AMS = ZoneInfo("Europe/Amsterdam")
_NYSE = xcals.get_calendar("XNYS")
_SCREENER_TOP_N = 12


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

        # Update to today's regime
        self._update_regime()

        self._broker = broker
        self._portfolio = Portfolio(broker=broker, risk_cfg=self._cfg.risk)

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
            self._update_regime()
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

    def _update_regime(self) -> None:
        """Classify today's regime using the fitted model."""
        if self._market_data is None or not self._engine.is_fitted:
            return
        try:
            self._regime_state = self._engine.current_regime(
                self._market_data, self._feature_cfg
            )
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
        except Exception as exc:
            log.warning("Regime update failed: %s", exc)

    # ------------------------------------------------------------------
    # Morning pipeline (regime-aware)
    # ------------------------------------------------------------------

    def run_morning_pipeline(self) -> None:
        if not _NYSE.is_session(date.today().isoformat()):
            log.info("Market closed — skipping morning pipeline")
            return

        self._maybe_rolling_refit()
        self._update_market_data()
        self._update_regime()
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

        # --- Scrape (always, for DB persistence) ------------------------
        new_disclosures = run_scraper()
        qualified = filter_disclosures(new_disclosures)
        log.info("Disclosures: %d new, %d qualified", len(new_disclosures), len(qualified))

        if not _at_capacity:
            # --- Regime state as gate -----------------------------------
            if self._regime_state is None:
                log.warning("No regime state — processing signals without regime filter")

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

            congress_tickers: set[str] = {disc["ticker"] for disc in qualified}

            for disc in qualified:
                if not self._portfolio.can_open_new_position():
                    log.info("Position limit reached — stopping")
                    break
                try:
                    self._process_signal(disc, sector_allocation)
                except Exception:
                    log.exception("Failed processing %s — skipping", disc.get("ticker", "?"))

            # ── Phase 2: fundamental screener (regime-aware) ─────────────────────
            try:
                universe = list(get_universe())
                candidates = run_factor_screen(universe, top_n=_SCREENER_TOP_N)
                already_open = (
                    {p["ticker"] for p in self._broker.get_positions()}
                    | {pos["ticker"] for pos in get_open_positions()}
                )

                for candidate in candidates:
                    if not self._portfolio.can_open_new_position():
                        log.info("Position limit reached — stopping Phase 2")
                        break
                    if candidate.ticker in already_open:
                        continue
                    try:
                        opened = self._process_fundamental_candidate(
                            candidate, sector_allocation, congress_tickers
                        )
                        if opened:
                            already_open.add(candidate.ticker)
                    except Exception:
                        log.exception(
                            "Failed processing fundamental candidate %s — skipping",
                            candidate.ticker,
                        )
            except Exception:
                log.exception("Phase 2 fundamental screener failed — skipping")

        # ── Phase 3: inverse ETF hedge pass ─────────────────────────
        if is_hedge_now:
            self._run_hedge_pass()
        # ─────────────────────────────────────────────────────────────

    def _process_signal(self, disc: dict, sector_allocation: dict) -> None:
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
            return

        research = gather_research(ticker)

        # AI entry scoring (unchanged from existing bot)
        score: EntryScore = score_entry_with_debate(
            disc, committees=committees, sector=sector,
            lag_days=lag, estimated_cost_pct=0.05,
            research=research, cluster_count=cluster_count,
        )
        if score.entry != "buy":
            log.info("Skipping %s: AI conviction %d", ticker, score.conviction)
            return

        # Regime allocation scaling
        base_pct = score.position_pct
        if self._regime_state is not None:
            alloc_decision = self._alloc.compute(ticker, base_pct, self._regime_state)
            final_pct = alloc_decision.final_position_pct
            if final_pct < 0.1:
                emit_event(log, EventType.SIGNAL_REJECTED,
                           f"{ticker} blocked by regime allocation ({alloc_decision.rationale})")
                return
        else:
            final_pct = base_pct

        # Risk manager veto
        entry_price_info = yf.Ticker(ticker).info
        entry_price = entry_price_info.get("regularMarketPrice", 0)
        if not entry_price:
            log.warning("No price for %s — skipping", ticker)
            return

        position_size_usd = self._broker.get_cash() * final_pct / 100
        adv_usd = research.avg_daily_volume_usd if research else None
        veto = self._risk.validate_order(
            ticker=ticker, position_pct=final_pct, sector=sector,
            sector_allocation=sector_allocation,
            position_size_usd=position_size_usd, adv_usd=adv_usd,
        )

        if not veto.allowed:
            emit_event(log, EventType.RISK_VETO,
                       f"{ticker} vetoed: {veto.reason}")
            return

        # Apply size multiplier from risk state (e.g. 0.5× during daily drawdown)
        final_pct *= veto.size_multiplier

        signal_id = insert_signal(
            disc["id"], ticker, score.conviction,
            final_pct, score.rationale, list(score.risk_flags),
        )
        self._portfolio.open_position(
            ticker=ticker, position_pct=final_pct,
            signal_id=signal_id, rationale=score.rationale, entry_price=entry_price,
        )
        sector_allocation[sector] = sector_allocation.get(sector, 0.0) + final_pct
        emit_event(log, EventType.ORDER_PLACED,
                   f"Opened {ticker} pct={final_pct:.1f}% conv={score.conviction}",
                   data={"ticker": ticker, "pct": final_pct,
                         "regime": self._regime_state.regime_label if self._regime_state else "?",
                         "conviction": score.conviction})

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
            estimated_cost_pct=0.05,
            research=candidate.research,
            signal_type=signal_type,
            factor_score=candidate.composite_score,
            ticker=ticker,
        )

        if score.entry != "buy":
            log.info("Skipping %s (%s): conviction %d", ticker, signal_type, score.conviction)
            return False

        base_pct = score.position_pct
        if self._regime_state is not None:
            alloc_decision = self._alloc.compute(ticker, base_pct, self._regime_state)
            final_pct = alloc_decision.final_position_pct
            if final_pct < 0.1:
                emit_event(
                    log, EventType.SIGNAL_REJECTED,
                    f"{ticker} blocked by regime ({alloc_decision.rationale})",
                )
                return False
        else:
            final_pct = base_pct

        entry_price = yf.Ticker(ticker).info.get("regularMarketPrice", 0)
        if not entry_price:
            log.warning("No price for %s — skipping", ticker)
            return False

        position_size_usd = self._broker.get_cash() * final_pct / 100
        adv_usd = candidate.research.avg_daily_volume_usd if candidate.research else None
        veto = self._risk.validate_order(
            ticker=ticker,
            position_pct=final_pct,
            sector=sector,
            sector_allocation=sector_allocation,
            position_size_usd=position_size_usd,
            adv_usd=adv_usd,
        )

        if not veto.allowed:
            emit_event(log, EventType.RISK_VETO, f"{ticker} vetoed: {veto.reason}")
            return False

        final_pct *= veto.size_multiplier

        self._portfolio.open_position(
            ticker=ticker,
            position_pct=final_pct,
            signal_id=None,
            rationale=score.rationale,
            entry_price=entry_price,
            signal_source=signal_type,
        )
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
                    entry_price = yf.Ticker(order.ticker).info.get("regularMarketPrice", 0)
                    if not entry_price:
                        log.warning("No price for hedge ETF %s — skipping", order.ticker)
                        continue
                    self._portfolio.open_position(
                        ticker=order.ticker,
                        position_pct=order.position_pct,
                        signal_id=None,
                        rationale=order.rationale,
                        entry_price=entry_price,
                        signal_source="hedge",
                    )
                    emit_event(
                        log, EventType.HEDGE_ENTRY,
                        f"Opened hedge {order.ticker} pct={order.position_pct:.1f}%",
                        data={
                            "ticker": order.ticker,
                            "position_pct": order.position_pct,
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
                    exit_price = yf.Ticker(ticker).info.get("regularMarketPrice", 0)
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
        for pos in get_open_positions():
            if pos.get("signal_source") == "hedge":
                continue
            try:
                info = yf.Ticker(pos["ticker"]).info
                current_price = info.get("regularMarketPrice", pos["entry_price"])
                days_held = (date.today() - date.fromisoformat(pos["entry_date"])).days
                research = gather_research(pos["ticker"])
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
                    log.info("Reduced %s: %s", pos["ticker"], decision.rationale)
            except Exception:
                log.exception("Exit review failed for %s", pos.get("ticker", "?"))

    # ------------------------------------------------------------------
    # EOD
    # ------------------------------------------------------------------

    def run_eod(self) -> None:
        self._portfolio.log_snapshot()
        try:
            equity = self._broker.get_equity() if hasattr(self._broker, "get_equity") \
                else self._broker.get_cash()
            self._risk.check_circuit_breakers(equity)
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
        scheduler = BlockingScheduler(timezone=_AMS)
        scheduler.add_job(refresh_universe, "cron", day_of_week="mon", hour=7, minute=0)
        scheduler.add_job(self.run_morning_pipeline, "cron", hour=14, minute=0)
        scheduler.add_job(self.run_exit_review, "cron", hour=15, minute=0)
        scheduler.add_job(self.run_eod, "cron", hour=22, minute=30)
        scheduler.add_job(log_weekly_report, "cron", day_of_week="fri", hour=22, minute=45)
        log.info("Regime-aware scheduler started (Amsterdam timezone)")
        scheduler.start()
