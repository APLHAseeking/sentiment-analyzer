# Inverse ETF Hedge Analysis

## The Problem: Volatility Decay

Daily-rebalanced inverse ETFs (SH, PSQ, RWM) suffer from compounding decay:
- A daily -1× ETF on an asset with 20% annualised vol loses ~2% per year from decay alone
- In choppy/ranging markets, whipsaw compounds the drag further
- The current system holds SH for extended bear periods — the ETF's track record vs SPY over
  multi-week holding periods systematically underperforms the theoretical −1× return

## Alternatives Considered

### 1. Keep inverse ETFs (current)
**Pros:** Simple, liquid, no margin/derivatives account needed
**Cons:** Volatility decay eats 1-4% per year; multi-week holds amplify this

### 2. Gross-position reduction (recommended to evaluate)
**Approach:** When regime flips to bear/crash, reduce long positions by 30-50% rather than
adding inverse ETFs. Re-enter when regime normalises.
**Pros:** Zero decay, no additional positions to manage, cleaner NAV accounting
**Cons:** May miss short recovery bounces; requires rethinking the regime transition logic

### 3. Defined-risk puts (out of scope without options account)
**Approach:** Buy protective puts on SPY at strike = current_price × 0.95 for 30-day cover
**Pros:** Defined risk, no decay, leveraged protection
**Cons:** Requires options approval; premium cost; complexity

## Empirical Analysis
[Run `backtesting/analyze_hedge_drag.py` with real SPY data to populate this section]

Synthetic data results (for structure validation only):
- The `simulate_inverse_etf_decay` function models a 252-day window of GBM returns
  (mu=7% ann, σ=18% ann) and compares daily-compounded inverse-ETF NAV to the
  theoretical static −1× exposure.  Expected decay ≈ T × σ²/2.
- The `compare_hedge_strategies` function runs both approaches over the bear-flagged
  sub-periods (drawdown > 15% from peak) and returns total return, annualised return,
  Sharpe, and max drawdown for each.
- Run `python backtesting/analyze_hedge_drag.py` to see the full output table.

## Recommendation
**Cannot finalize without real backtest data.**

When real data is available, compare:
- Bear-period total return: inverse ETF approach vs gross reduction
- Cumulative decay drag over the full backtest period
- Transaction cost comparison (inverse ETF has daily bid-ask on turnover vs one-time reduce)

**Preliminary recommendation:** Evaluate gross-position reduction as the primary bear-regime
response before adding inverse ETF positions. Only add inverse ETFs if gross reduction
consistently underperforms by > 2% per bear-regime episode.

## Next Steps
1. Run `python backtesting/analyze_hedge_drag.py` with real SPY history
2. Document results in this file
3. If gross reduction wins by > 1% annualised → raise a PR to replace hedge_engine logic
