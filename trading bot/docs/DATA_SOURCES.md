# External Data Sources

All external dependencies consumed by the trading bot, their current status, and fallback behaviour.

| Source | Purpose | Status | URL | Fallback |
|--------|---------|--------|-----|----------|
| Capitol Trades JSON API | Congressional trade disclosures (primary) | Active (SPA — JSON endpoint) | `https://capitoltrades.com/api/trades?page={n}&pageSize=100` | HTML scraper fallback (`bot/scraper.py` tier 2); cached JSON snapshot (`capitol_trades_merged.json`) |
| Capitol Trades HTML | Congressional trade disclosures (fallback tier) | Unreliable (JS SPA renders empty tables) | `https://capitoltrades.com/trades` | Dead-feed alert fired; pipeline receives zero congressional signals |
| unitedstates/congress-legislators | Committee membership (YAML) | Active (GitHub-hosted, maintained) | `https://raw.githubusercontent.com/unitedstates/congress-legislators/main/committee-membership-current.yaml` (+ `legislators-current.yaml`, `committees-current.yaml`) | Stale shelve disk cache (`propublica_committee_cache`); returns empty tuple on total failure |
| yfinance | Prices, fundamentals, VIX, universe data | Active | PyPI `yfinance` | None — single point of failure. Callers fall back to `0.0`/skip silently; treat `None` as a first-class failure |
| S&P 500 Wikipedia | Universe constituents | Active | `https://en.wikipedia.org/wiki/List_of_S%26P_500_companies` | `universe_cache.json` |
| iShares Russell 1000 ETF | Universe constituents | **Accepted-not-pursued (2026-07-23)** — endpoint serves bot-protection HTML, not CSV, confirmed live | `https://www.ishares.com/us/products/239707/IWB` (CSV download) | Falls back to S&P 500 (`bot/universe.py::_build_universe`) — this is now the bot's permanent scope, not a degraded state |
| Ken French Data Library | Factor returns (Fama-French attribution) | Active | `https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html` | Manual CSV file (`ff_factors.csv`) |
| Alpaca Paper API | Order execution (paper only) | Active (paper only — live disabled) | `https://paper-api.alpaca.markets` | `SimulatedBroker` (`execution/paper_broker.py`) |

## Notes

- **Capitol Trades** switched to a JavaScript SPA; the static-HTML scraper (`_parse_trades_page`) reliably returns 0 rows in production. The JSON API endpoint (`_fetch_page_json`) is attempted first; if it returns a non-JSON response or a 4xx error the HTML scraper is used as a secondary fallback. If both return nothing, a `DEAD_FEED` alert is emitted and the congressional signal pipeline receives zero inputs for that run.
- **ProPublica Congress API** was discontinued in 2024. `bot/committee.py` now uses the `unitedstates/congress-legislators` GitHub YAML files instead (no API key required). The 30-day shelve disk cache insulates the bot from transient GitHub outages.
- **yfinance** has no official SLA and rate-limits aggressively. It is used for prices, fundamentals (`info`), and VIX data. Missing data should be treated as a first-class failure — do not silently substitute `0.0` for fundamentals that drive signals.
- **Survivorship bias**: the S&P 500 and Russell 1000 universe lists reflect *current* composition. Backtests over historical periods are biased toward survivors. See `docs/PIT_DATA_REQUIREMENTS.md` for the point-in-time data strategy.
- **Russell 1000 (accepted-not-pursued, 2026-07-23)**: broadening past S&P 500 needs a new data-provider integration (e.g. FMP) with zero existing code today, plus a paid account of unknown cost — no free/no-signup source was found viable after ~7 attempts across sessions (iShares, FTSE Russell, stockanalysis.com, SlickCharts). Closed out as a deliberate scope decision rather than a lingering open bug: the bot trades S&P 500 only, which already covers the more liquid, better-data-covered names the factor screener relies on. Revisit only if a concrete, costed data source is identified.
