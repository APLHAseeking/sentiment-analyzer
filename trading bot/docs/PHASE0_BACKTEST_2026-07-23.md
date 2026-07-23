# Phase 0 PIT Backtest — Report (2026-07-23)

Implements the plan referenced in `docs/BOT_REVIEW_2026-07-20.md`'s item 3 (of 6).
Tests the bot's **primary signal** — the full fundamental factor composite
(value/momentum/quality/low-vol/reversal, `screener/factor_scorer.py`'s
`_build_factor_df`/`_compute_composite`) — against real point-in-time data
for the first time. Recommendation only — `screener/factor_scorer.py` was
**not** touched by this work.

## Confirmed spec (recap)

- **Gate (pre-committed before results were seen, per `docs/PHASE0_FINDINGS.md`):**
  t-stat > 2 AND IR > 0.5 on daily excess return over SPY, stable across
  periods (no sign flip between first and second half of the sample).
- **Decision rule:** gate fails → stop, do not add complexity, write up the
  finding. Gate clears → proceed to Phase 1 (already implemented).
- **Universe:** PIT S&P 500 membership from `fja05680/sp500`
  (`backtesting/pit_constituents.py`), not current constituents projected
  backward.
- **Fundamentals:** SimFin free tier (`screener/simfin_fundamentals.py`) —
  real filing/publish dates, not today's restated values.
- **Prices:** yfinance first, Tiingo fallback for tickers yfinance has no
  data for (`market_data/pit_prices.py`).
- **Sample window: 2021-09-01 to 2025-06-30.** Bounded on the fundamentals
  side by SimFin's free-tier history (2020-08-31 to 2025-06-30, confirmed
  empirically this session) plus a ~1-year run-in required before any
  ticker can have a full trailing-4-quarter window for the ratios that need
  one (P/E, ROE, profit margin, free cash flow). This is a **real,
  disclosed limitation**: ~3.75 years and 16 quarterly rebalances is a
  thinner sample than an ideal multi-decade backtest would give — fewer
  non-overlapping walk-forward windows, less confidence the result
  generalizes across regimes the sample didn't include. Not hidden.
- **IR:** gross of transaction costs (matches `run_pit_backtest`'s built-in
  slippage/commission model, not a separately-modeled cost layer) —
  validates signal content, not a live-achievable number.
- **HAC bandwidth:** 21 trading days (~1 month), a defensible default for a
  quarterly-rebalanced book's daily-return autocorrelation, chosen before
  running the gate — not tuned to the result.

## Sample construction

| Stage | Tickers |
|---|---|
| PIT S&P 500 universe, 2021-09-01 to 2025-06-30 | 576 |
| With price data (yfinance or Tiingo) | 560 (97.2%) |
| With at least one fundamental snapshot | 461 (80.0%) |

**16 tickers (2.8%) had no price data in either source** — an honest,
disclosed gap, not silently dropped: `ABC, ANTM, BF.B, BK, BLL, BRK.B, CDAY,
COG, FBHS, FRC, GPS, MMC, PKI, RE, VIAC, WLTW`. Mostly ticker-rename/merger
cases (ANTM→ELV, CDAY acquired, COG merged, WLTW→WTW) plus dotted-ticker
formatting mismatches (BRK.B, BF.B) — not a sign of a broken fetcher, and
consistent with Tiingo's own confirmed-partial delisted-ticker coverage
from earlier this session.

**A second, more consequential gap was found and fixed while preparing this
report, not left as a footnote.** The first full run left only 429/576
tickers (74.5%) with any fundamentals at all — investigated rather than
accepted, and found concentrated in one sector: 146 missing tickers
included AIG, AXP, BAC, C, BK, CB and more, all financials. SimFin reports
banks and insurers on separate `income-banks`/`income-insurance` (etc.)
datasets with materially different statement structures — the original
fetch only pulled the generic dataset, silently excluding an entire sector
from ever being scored rather than letting it compete on the merits.
Confirmed empirically that all 3 variants (generic/banks/insurance) are
free-tier accessible and share the same core columns needed, then merged
all 3. Coverage improved to 461/576 (80.0%); a few names (C, BK confirmed
directly) are genuinely absent from SimFin's free tier entirely — not a
bug, checked rather than assumed. Re-running the full backtest after this
fix changed the result only marginally (t-stat -1.75 vs -1.77 before the
fix, IR -0.78 vs -0.79) — reassuring: the finding isn't an artifact of the
missing-financials gap.

319 signals were generated across 16 quarterly rebalance windows (top-20
composite-score names each quarter), producing 58 realized trades.

## Results

### Gate — HAC/Newey-West, bandwidth = 21 trading days

| Metric | Value | Gate (t>2 AND IR>0.5) |
|---|---|---|
| Mean daily excess return | -0.0495% | — |
| t-stat | **-1.75** | **FAIL** |
| Information ratio (annualized) | **-0.78** | **FAIL** |
| n (trading days) | 941 | |

The point estimate is not merely statistically insignificant — it is
**negative**. The strategy underperformed SPY on both a raw and a
risk-adjusted (IR) basis over this sample.

### Stability — first half vs second half of the sample

| Half | t-stat | IR | n (days) |
|---|---|---|---|
| First half | -0.49 | -0.33 | 471 |
| Second half | **-2.00** | -1.20 | 470 |

Both halves are negative — no sign flip, so this isn't a case of a
first-half/second-half reversal masking a real effect. If anything the
underperformance was more pronounced in the second half.

### Supplementary: multi-factor attribution (diagnostic, not a second gate)

| | Value |
|---|---|
| Alpha (annualized) | +9.59% |
| Alpha t-stat | 0.95 (not significant) |
| R² | 0.29 |
| Beta (Mkt-RF) | 0.24 (t=5.55) |
| SMB | -0.02 (t=-0.51) |
| HML | 0.06 (t=2.58) |
| Momentum | -0.01 (t=-0.25) |

**Caveat on this table**: this is a Fama-French + momentum regression on
the strategy's own realized daily returns — a different methodology from
the primary SPY-relative gate above (it controls for the strategy's
realized market beta, which came out quite low at 0.24, consistent with
the low-vol sleeve's intended defensive tilt). After that adjustment, the
point estimate is *positive* but nowhere near significant (t=0.95). This
is useful context for *why* the raw SPY-relative number is so negative —
substantially due to running at much lower market exposure than SPY during
a period markets rose — but it does **not** override the pre-committed
gate, which is explicitly about raw SPY-relative excess return, not a
beta-adjusted number. Same discipline as the SUE backtest's regime
breakdown: reported as diagnostic context, not substituted for the actual
decision rule.

## Recommendation

**Gate fails** — on both the primary t-stat/IR test and the stability
condition. Per the pre-committed decision rule:

> **The Phase 0 gate does not clear. Do not proceed to add complexity —
> the bot's live fundamental factor composite has not demonstrated a
> statistically positive edge over SPY in this real, point-in-time-correct
> sample.**

This is a genuine negative result, not a data problem or a bug masquerading
as one: two real data gaps were found and either fixed (the financials
sector, confirmed to barely move the result) or fully disclosed and
investigated rather than assumed (the 16 missing-price tickers, the
genuinely-SimFin-absent names). The sample is real S&P 500 point-in-time
membership, real filing-date-correct fundamentals, and real (or honestly
gapped) prices — not today's survivor-only data projected backward.

**What this does and doesn't mean:**
- It does **not** mean every individual factor sleeve (value, momentum,
  quality, low-vol, reversal) is worthless — this tests the *composite*,
  over one ~3.75-year sample that included a rate-hiking bear market
  (2022) and a strong recovery. A defensively-tilted (low-beta) composite
  underperforming a rising market by this much is a real, measurable
  outcome, not automatically evidence the underlying factor logic is
  broken.
- It does mean the specific numbers previously cited in this repo's own
  design docs (e.g. residual-momentum Sharpe ~0.88 from
  `docs/FACTOR_BACKTEST_2026-06-28.md`) were computed on non-PIT,
  survivor-biased data and should not be treated as validated — this
  backtest is the first real, PIT-correct read, and it does not confirm
  those earlier numbers.
- The sample is thinner than ideal (16 rebalances, one bear/recovery
  cycle) — a longer PIT history, if it ever becomes available beyond
  SimFin's free-tier 5-year cap, would be a more decisive test either way.

No code change follows from this report. `screener/factor_scorer.py`
remains untouched, matching the same commitment the SUE PIT backtest made
and kept.
