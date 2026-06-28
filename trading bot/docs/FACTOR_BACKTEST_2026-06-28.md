# Price-Factor Backtest Findings — 2026-06-28

Source: `backtesting/backtest_price_factors.py` (point-in-time, monthly rebalance,
equal-weight top-30% long, 2019-02 → 2026-06). Universe is a fixed sector-diversified
large/mid-cap set, so results are **survivorship-biased and indicative, not a clean
OOS test** (see `docs/PHASE0_FINDINGS.md`). These findings are encoded directly into
`screener/factor_scorer.py::_REGIME_WEIGHTS` so live scoring reflects them.

| Strategy | TotRet% | AnnRet% | Vol% | Sharpe | Sortino | MaxDD% | Alpha%/yr | Beta |
|---|---|---|---|---|---|---|---|---|
| Residual Momentum | 306.8 | 20.9 | 19.2 | **0.88** | 1.26 | 28.9 | **+5.79** | 0.86 |
| Equal-Weight (baseline) | 233.4 | 17.7 | 18.7 | 0.75 | 1.06 | 34.8 | +2.34 | 0.91 |
| Low-Vol / BAB | 134.2 | 12.2 | 15.6 | 0.56 | 0.80 | **29.0** | +0.91 | **0.61** |
| Mean Reversion (1m) | 168.7 | 14.3 | 22.9 | 0.53 | 0.75 | 43.5 | −1.24 | 1.03 |
| SPY (buy & hold) | 201.8 | 16.1 | 19.5 | 0.66 | 0.93 | 33.7 | — | — |

## What the bot does with this

- **Residual momentum — strongest single sleeve.** Highest Sharpe and the only sleeve
  with large positive alpha. Lives inside the momentum sleeve, which is weighted heavily
  in trending regimes (bull/euphoria/melt-up: momentum weight 0.40).
- **Low-vol / BAB — defensive, not a return engine.** Lower absolute return but the
  lowest beta (0.61) and a drawdown below SPY. Weighted **up in bear/crash** (0.15–0.20)
  for capital preservation, down in strong rallies (0.05) where low-beta names lag.
- **Mean reversion (1-month) — weak standalone; kept small and regime-gated.** Naive
  short-term reversal on this large-cap, monthly-rebalance universe **underperformed**
  (Sharpe 0.53, negative alpha, deepest drawdown). The documented reversal premium is
  concentrated in small caps / higher frequency and is prone to "falling knives." So it
  is **not** given a standalone allocation: it is one sub-sleeve weighted ~0.15 only in
  neutral/range-bound and bear regimes, ~0.05 in trends, and is **blended** with
  quality/value/low-vol in the composite — i.e. the bot prefers *oversold names that are
  also fundamentally sound*, which mitigates the falling-knife problem the standalone
  test exposes.

## Caveats / follow-ups

- Survivorship bias inflates every line (even the baseline beats SPY). Treat relative
  ranking, not absolute returns, as the signal.
- Reversal could plausibly be improved (shorter horizon, small-cap tilt, quality filter);
  not pursued here. Current handling is deliberately conservative.
- Fundamentals-based sleeves (value/quality) are **not** in this backtest — they read
  *current* yfinance `.info` and are not historically reconstructable.
