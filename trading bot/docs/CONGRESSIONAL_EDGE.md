# Congressional Signal Edge Analysis

## Status

Analysis harness implemented in `backtesting/analyze_congressional_edge.py`. The
synthetic-data preliminary findings below predate a real-cached-data test run
2026-07-17 (not through this harness — against the live bot's actual scraped
disclosure history) that found a significantly negative excess return; see
"## Decision (2026-07-22)" below for the outcome. `docs/PIT_DATA_REQUIREMENTS.md`
still applies if this signal is ever re-evaluated on a longer real PIT history.

Run the demonstration (offline, synthetic data):

```bash
cd "trading bot"
python -m backtesting.analyze_congressional_edge
```

---

## Method

### Variants compared

| Variant | Description |
|---------|-------------|
| Factor only | Fundamental factor screener baseline (no congressional signals) |
| +Congress lag≤7 | Factor signals + congressional disclosures filed ≤ 7 days ago |
| +Congress lag≤14 | Factor signals + congressional disclosures filed ≤ 14 days ago |
| +Congress lag≤30 | Factor signals + congressional disclosures filed ≤ 30 days ago |
| +Congress lag≤45 | Factor signals + congressional disclosures filed ≤ 45 days ago |

### Cost assumptions

- 10 bps one-way slippage (fills worse than mid by 0.10%)
- 0.05% round-trip commission
- These are consistent with `run_strategy_backtest.run_pit_backtest` defaults

### Signal merging rule

- If a congressional ticker already appears in the factor top-N on the same rebalance date, the factor signal is kept unchanged (no double-counting of size).
- Congressional signals for tickers **not** in the factor top-N are appended as additional positions at the congressional `position_pct`.
- This is conservative and mirrors the live engine's `"both"` signal-type logic.

### Metrics reported

- **Sharpe** — annualised Sharpe ratio (risk-free = 4%)
- **Alpha%** — Jensen's alpha vs SPY (annualised %, requires SPY price series)
- **IR** — Information ratio vs SPY (requires SPY price series)
- **Return%** — cumulative total return over the sample
- **MaxDD%** — maximum peak-to-trough drawdown
- **Signals** — total number of position signals generated

---

## Preliminary Findings (synthetic data)

> **These numbers use synthetic random-walk prices and are for demonstration only.**
> Replace the CSVPITProvider fixtures with real PIT data before drawing conclusions.

The harness runs cleanly offline and produces a comparison table such as:

```
Variant              Sharpe   Alpha%   IR     Return%   MaxDD%  Signals
Factor only          0.xxx    x.xx%    x.xxx   xx.xx%   xx.xx%  60
+Congress lag<=7     0.xxx    x.xx%    x.xxx   xx.xx%   xx.xx%  63
+Congress lag<=14    0.xxx    x.xx%    x.xxx   xx.xx%   xx.xx%  65
+Congress lag<=30    0.xxx    x.xx%    x.xxx   xx.xx%   xx.xx%  71
+Congress lag<=45    0.xxx    x.xx%    x.xxx   xx.xx%   xx.xx%  78
```

With synthetic data the congressional layer neither reliably adds nor subtracts alpha —
the results are noise-driven and reset on every run. This is the expected null result
before real data is supplied.

---

## Recommendation

**Cannot be determined without real PIT data.**

Once real PIT data and a genuine congressional disclosure history (with known
`disclosure_date` and `trade_date` fields) are supplied, apply the following
decision rules:

| Condition | Action |
|-----------|--------|
| Incremental alpha (congress − factor-only) < 0 after costs, for **all** lag variants | Drop congressional layer entirely |
| IR < 0.3 for **all** lag variants | Drop congressional layer |
| Lag≤7 dominates (highest Sharpe increment) | Set `max_lag_days = 7` in `UniverseConfig` |
| Lag≤14 dominates | Set `max_lag_days = 14` |
| t-stat of alpha increment < 2.0 over the full sample | Do not rely on signal; conduct walk-forward before trusting it |

### Decision (2026-07-22)

The 2026-07-17 real-cached-data test (see `docs/CLAUDE-REFERENCE.md#history`) found
excess return of **-0.636% at 1mo (t=-2.57)** and **-2.538% at 3mo (t=-4.93)** —
negative and statistically significant at both horizons, satisfying the first
decision rule above ("Incremental alpha < 0 after costs → Drop congressional
layer") on its own. **Signal disabled**: `Settings.congressional.enabled` added
(default `False`) in `system/config.py`, gating both the Phase 2 congressional-entry
logic and the Phase 1 "both"-signal-type conviction boost in
`orchestration/main_loop.py` — see that file's history entry for the same date.
Scraping itself is unaffected and keeps running (data collection, not signal use),
in case a longer history or different filtering approach is worth revisiting later.

### Current known data limitations

1. **Capitol Trades is a JavaScript SPA.** The static-HTML scraper in `bot/scraper.py`
   likely returns 0 rows in production — congressional feed can be silently empty.
   `run_1year_backtest.py` reads a cached JSON snapshot (Oct 2025 – May 2026 only).

2. **The cached JSON covers only ~7 months.** This is too short for statistically
   reliable alpha estimation. A minimum of 3 years of disclosure data is recommended.

3. **Lag days are not currently recorded** in the DB schema or the cached JSON.
   `lag_days` must be computed as `rebalance_date − disclosure_date` before feeding
   signals into this harness.

---

## What Would Prove the Edge

To be confident the congressional layer adds value, the following criteria should all hold:

- **Statistically positive incremental alpha**: t-stat > 2.0 (two-sided) net of costs
- **IR > 0.5** relative to the factor-only baseline
- **Results stable across sub-periods** (e.g., walk-forward validation with at least
  3 non-overlapping windows)
- **Monotone lag decay**: alpha should be higher at shorter lags (≤7) than longer ones
  (≤45), consistent with the hypothesis that disclosures carry information that decays
  quickly. A flat or inverted relationship is a red flag.

---

## Files

| File | Purpose |
|------|---------|
| `backtesting/analyze_congressional_edge.py` | Analysis harness (runnable offline) |
| `docs/PIT_DATA_REQUIREMENTS.md` | Schema requirements for real PIT data |
| `backtesting/run_strategy_backtest.py` | PIT backtest runner (Task 0.4) |
| `backtesting/pit_data.py` | PITDataProvider abstraction + CSVPITProvider |
