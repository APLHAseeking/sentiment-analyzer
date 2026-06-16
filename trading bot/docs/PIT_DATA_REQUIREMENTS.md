# PIT Data Requirements

This document specifies the data files required to run the survivorship-free,
point-in-time (PIT) backtest via `CSVPITProvider` and `run_pit_backtest()`.

---

## Why PIT Data?

`yfinance .info` returns **current** fundamentals. Using it in a historical backtest
means scoring a 2020 trade with 2026 P/E ratios — look-ahead bias. Every historical
performance number produced this way is untrustworthy.

PIT fundamentals respect reporting lag: a Q1 2020 filing published in May 2020 should
not be visible in a simulation until May 2020, not March 2020.

---

## Required Files

### 1. `constituents.csv` — Index membership (survivorship-free)

| Column | Type   | Description |
|--------|--------|-------------|
| `date` | ISO date | Snapshot date for this membership list |
| `ticker` | str | Ticker symbol (uppercase) |

One row per (date, ticker) pair. Multiple snapshots are supported; `CSVPITProvider`
uses the most recent snapshot on or before the requested date.

**Example:**
```
date,ticker
2019-12-31,AAPL
2019-12-31,MSFT
2019-12-31,AMZN
2020-06-30,AAPL
2020-06-30,MSFT
2020-06-30,TSLA
```

**Why it matters:** Without PIT membership data, stocks that were delisted or removed
from the index (often after poor performance) appear in historical "possible trades",
inflating historical returns — survivorship bias.

**Where to get it:**
- S&P 500: Compustat (Wharton WRDS), Norgate Data, or a pre-built CSV from
  academic sources (e.g. the Barra US Equity model historical constituents).
- Russell 1000: iShares historical composition (monthly snapshots available via ETF filings).
- Free approximation: github.com/fja05680/sp500 (monthly S&P 500 changes since 1996).

---

### 2. `fundamentals.csv` — Point-in-time fundamental snapshots

| Column | Type | Description |
|--------|------|-------------|
| `date` | ISO date | First date this snapshot was publicly available (filing date, not quarter-end) |
| `ticker` | str | Ticker symbol |
| `trailingPE` | float | Trailing 12-month P/E ratio |
| `priceToBook` | float | Price-to-book ratio |
| `freeCashflow` | float | Free cash flow (absolute, e.g. 1.2e10) |
| `marketCap` | float | Market capitalisation in USD |
| `returnOnEquity` | float | ROE (decimal, e.g. 0.15 = 15%) |
| `profitMargins` | float | Net profit margin (decimal) |
| `debtToEquity` | float | Debt-to-equity ratio |
| `sector` | str | GICS sector (e.g. "Technology") |

Column names match `yfinance .info` keys so the existing `_build_factor_df()` scoring
function works without changes.

**Reporting lag:** Use the SEC filing date (available date) not the quarter-end date.
Typical lag: 40–75 days for 10-Qs, 60–90 days for 10-Ks.

**Example:**
```
date,ticker,trailingPE,priceToBook,freeCashflow,marketCap,returnOnEquity,profitMargins,debtToEquity,sector
2020-05-01,AAPL,22.5,11.2,5.8e10,1.3e12,0.61,0.21,1.73,Technology
2020-05-01,MSFT,31.0,12.0,4.5e10,1.4e12,0.40,0.35,0.62,Technology
```

**Where to get it:**
- **Sharadar (via Nasdaq Data Link):** Best value for individual researchers (~$40/mo).
  Provides point-in-time financials with filing-date accuracy.
- **Compustat (WRDS):** Academic standard. Available via most university libraries.
- **Tiingo Fundamentals:** Available via API with reasonable pricing.
- **SimFin:** Free tier available for basic fundamentals (annual/quarterly).
  URL: https://simfin.com/

---

### 3. `prices.csv` — Split/dividend-adjusted daily closes (wide format)

| Column | Type | Description |
|--------|------|-------------|
| `date` | ISO date | Trading date |
| `TICKER` | float | Split and dividend-adjusted close for that ticker |

Wide format: one row per date, one column per ticker. **Must include delisted names**
for their live trading period (the data should not stop when a stock is removed from
an index — it should stop when the stock was delisted or acquired).

**Example:**
```
date,AAPL,MSFT,ENRN
2001-01-02,0.94,21.3,72.5
2001-01-03,0.95,21.8,71.0
...
2001-12-01,0.96,22.0,
```
(ENRN prices stop when Enron was delisted — empty/NaN thereafter.)

**Where to get it:**
- **yfinance** covers actively-traded tickers well, but **delisted stocks are missing**.
  Use only if survivorship bias is acceptable for your research question.
- **Norgate Data:** Includes delisted stocks. Highly recommended for serious backtests.
- **Center for Research in Security Prices (CRSP):** Academic gold standard (expensive).
- **Tiingo:** Daily prices including some delisted tickers via their API.

---

### 4. `ff_factors.csv` — Fama-French + Momentum factors (for attribution)

Used by `backtesting/attribution.py`. Optional — only needed for factor attribution.

| Column | Type | Description |
|--------|------|-------------|
| `Date` | ISO date | Trading date |
| `MKT_RF` | float | Market excess return (decimal, not percent) |
| `SMB` | float | Small-minus-big factor |
| `HML` | float | High-minus-low (value) factor |
| `MOM` | float | Momentum factor (Carhart) |

**Note:** Ken French publishes in *percent* — divide by 100 before saving as this file.

**Where to get it:**
- Ken French Data Library: https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html
  - "Fama/French 3 Factors (Daily)" → MKT_RF, SMB, HML
  - "Momentum Factor (Daily)" → MOM
  Merge on date, divide all numeric columns by 100, save as `ff_factors.csv`.
- AQR Factors (QMJ, BAB, etc.): https://www.aqr.com/Insights/Datasets

---

## Minimal Fixture for Testing

To run `tests/test_pit_data.py` or smoke-test the harness without real data,
create a minimal fixture directory:

```
data/
  constituents.csv    # 3 tickers, 2 snapshot dates
  fundamentals.csv    # 3 tickers × 2 dates
  prices.csv          # 3 tickers × ~500 daily rows (include one "delisted" mid-sample)
```

The test suite includes in-memory synthetic fixtures and does not require files on disk.
See `tests/test_pit_data.py` for the expected formats.

---

## Phase 0 Gate Dependency

The Phase 0 gate (`TRADING_BOT_REVIEW_PLAN.md`) is **blocked on data** until at least
one of the above PIT data sources is supplied.

**Current acquisition status:** see the "Data Required Before Gate Can Open" table in
`docs/PHASE0_FINDINGS.md` — that table (not a separate checklist here) is the
canonical, currently-maintained status for all four datasets (§1-4 above). Update
that table directly as each dataset is acquired; do not duplicate it here.

The `CSVPITProvider` and `run_pit_backtest()` runner are fully implemented and tested
with synthetic data. The gate is blocked solely on supplying real vendor data.
