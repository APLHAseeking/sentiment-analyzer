import re
import time
import logging
import requests
from datetime import datetime, UTC
from bs4 import BeautifulSoup
from bot.db import get_existing_ids, insert_disclosures
from monitoring.logger import EventType, emit_event

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; congress-bot/1.0; research-only)"}
TRADES_URL = "https://capitoltrades.com/trades"
_JSON_URL = "https://capitoltrades.com/api/trades"
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Matches standard US tickers (AAPL) and class-share suffixes (BRK.B, BF.B)
_TICKER_RE = re.compile(r"^[A-Z]{1,5}([.\-][A-Z])?$")


def _normalize_ticker(raw: str) -> str:
    """Normalize Capitol Trades ticker format to yfinance format.

    Capitol Trades uses dots for class shares (BRK.B); yfinance expects hyphens (BRK-B).
    """
    return raw.replace(".", "-")


def _validate_trade(trade: dict) -> bool:
    """Validate a parsed trade dict. Expects ticker already uppercased."""
    if not trade.get("ticker") or not _TICKER_RE.match(trade["ticker"]):
        return False
    if not _ISO_DATE_RE.match(trade.get("transaction_date", "")):
        return False
    if not _ISO_DATE_RE.match(trade.get("disclosure_date", "")):
        return False
    if not trade.get("id"):
        return False
    return True


def _parse_json_response(data: dict) -> list[dict]:
    """Parse Capitol Trades JSON API response into trade dicts.

    The JSON shape (as captured from the XHR endpoint):
    {
        "data": [
            {
                "id": "abc123",
                "politician": {"name": "John Smith"},
                "issuer": {"ticker": "AAPL"},
                "txDate": "2025-10-15",
                "disclDate": "2025-10-20",
                "txType": "buy",
                "amount": "$50,001 - $100,000"
            },
            ...
        ]
    }
    """
    items = data.get("data") or data.get("trades") or []
    trades = []
    now_iso = datetime.now(UTC).isoformat()
    for item in items:
        try:
            trade_id = str(item.get("id", "")).strip()

            # Politician name — may be nested dict or plain string
            politician_raw = item.get("politician", "")
            if isinstance(politician_raw, dict):
                politician = politician_raw.get("name", "")
            else:
                politician = str(politician_raw)

            # Ticker — may be nested in "issuer" dict or plain string
            ticker_raw = item.get("ticker") or (
                item.get("issuer", {}).get("ticker", "") if isinstance(item.get("issuer"), dict)
                else item.get("issuer", "")
            )
            ticker = _normalize_ticker(str(ticker_raw).upper().strip())

            # Date fields — try multiple key names
            tx_date = (
                item.get("txDate") or item.get("transactionDate") or item.get("transaction_date", "")
            )
            discl_date = (
                item.get("disclDate") or item.get("disclosureDate") or item.get("disclosure_date", "")
            )

            # Transaction type
            tx_type = str(
                item.get("txType") or item.get("transactionType") or item.get("transaction_type", "")
            ).lower().strip()

            # Amount range
            amount = str(item.get("amount") or item.get("amount_range", "")).strip()

            if not trade_id or not ticker:
                continue

            trade = {
                "id": trade_id,
                "politician": politician,
                "ticker": ticker,
                "transaction_type": tx_type,
                "transaction_date": str(tx_date).strip(),
                "disclosure_date": str(discl_date).strip(),
                "amount_range": amount,
                "scraped_at": now_iso,
            }
            if _validate_trade(trade):
                trades.append(trade)
            else:
                log.warning("Skipping invalid JSON trade: %s", trade)
        except Exception as exc:
            log.warning("Error parsing JSON trade item %r: %s", item, exc)
    return trades


def _parse_trades_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table.q-table tbody tr")
    trades = []
    for row in rows:
        cells = row.select("td")
        if len(cells) < 7:
            continue
        trade_id = row.get("data-id", "").strip()
        ticker = _normalize_ticker(cells[2].get_text(strip=True).upper())
        if not trade_id or not ticker:
            continue
        trade = {
            "id": trade_id,
            "politician": cells[0].get_text(strip=True),
            "ticker": ticker,
            "transaction_type": cells[3].get_text(strip=True).lower(),
            "transaction_date": cells[4].get_text(strip=True),
            "disclosure_date": cells[5].get_text(strip=True),
            "amount_range": cells[6].get_text(strip=True),
            "scraped_at": datetime.now(UTC).isoformat(),
        }
        if _validate_trade(trade):
            trades.append(trade)
        else:
            log.warning("Skipping invalid trade row: %s", trade)
    return trades


def _fetch_page_json(page: int, max_retries: int = 3) -> list[dict]:
    """Try fetching trades from the Capitol Trades JSON API endpoint.

    Returns a list of parsed trade dicts, or raises on persistent failure.
    Returns an empty list if the endpoint returns an unexpected shape.
    """
    delay = 2.0
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(
                _JSON_URL,
                params={"page": page, "pageSize": 100},
                headers=HEADERS,
                timeout=30,
            )
            resp.raise_for_status()
            # Verify we got JSON (not HTML)
            content_type = resp.headers.get("Content-Type", "")
            if "json" not in content_type:
                log.debug(
                    "Capitol Trades JSON endpoint returned Content-Type=%r (not JSON) on page %d",
                    content_type, page,
                )
                return []
            data = resp.json()
            return _parse_json_response(data)
        except requests.exceptions.HTTPError as exc:
            # 4xx means the endpoint doesn't exist or requires auth — don't retry
            if exc.response is not None and exc.response.status_code < 500:
                log.debug("Capitol Trades JSON endpoint HTTP %d on page %d — falling back to HTML",
                          exc.response.status_code, page)
                return []
            last_exc = exc
        except (requests.exceptions.RequestException, ValueError) as exc:
            last_exc = exc
        if attempt < max_retries - 1:
            log.debug("Capitol Trades JSON fetch page %d failed (attempt %d/%d): %s — retrying in %.0fs",
                      page, attempt + 1, max_retries, last_exc, delay)
            time.sleep(delay)
            delay *= 2
    log.debug("Capitol Trades JSON endpoint unavailable after %d attempts: %s", max_retries, last_exc)
    return []


def _fetch_page(page: int, max_retries: int = 3) -> str:
    delay = 2.0
    for attempt in range(max_retries):
        try:
            resp = requests.get(
                TRADES_URL,
                params={"page": page, "pageSize": 100},
                headers=HEADERS,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.RequestException as exc:
            if attempt == max_retries - 1:
                raise
            log.warning("Capitol Trades fetch page %d failed (attempt %d/%d): %s — retrying in %.0fs",
                        page, attempt + 1, max_retries, exc, delay)
            time.sleep(delay)
            delay *= 2


def run_scraper(max_pages: int = 5) -> list[dict]:
    existing = get_existing_ids()
    new_trades: list[dict] = []
    for page in range(1, max_pages + 1):
        trades = _fetch_trades_for_page(page)
        if not trades:
            if page == 1:
                log.error(
                    "No trades parsed from Capitol Trades page 1 — HTML structure may "
                    "have changed and JSON endpoint may be unavailable. Signal pipeline "
                    "is receiving zero inputs. Manual inspection of %s required.", TRADES_URL
                )
                emit_event(log, EventType.DEAD_FEED,
                           "Capitol Trades scraper returned 0 trades on page 1 — signal pipeline dead",
                           alert=True)
            break
        fresh = [t for t in trades if t["id"] not in existing]
        new_trades.extend(fresh)
        if len(fresh) < len(trades):
            break
    if new_trades:
        insert_disclosures(new_trades)
    return new_trades


def _fetch_trades_for_page(page: int) -> list[dict]:
    """Two-tier fetch: try JSON endpoint first, fall back to HTML scraping."""
    # Tier 1: JSON API
    try:
        json_trades = _fetch_page_json(page)
        if json_trades:
            log.debug("Capitol Trades page %d: fetched %d trades via JSON API", page, len(json_trades))
            return json_trades
        elif json_trades is not None:
            # Empty list but no exception — JSON endpoint returned nothing or is unavailable
            log.debug("Capitol Trades JSON API returned 0 trades for page %d — trying HTML", page)
    except Exception as exc:
        log.debug("Capitol Trades JSON fetch raised unexpectedly: %s — falling back to HTML", exc)

    # Tier 2: HTML scraping fallback
    try:
        html = _fetch_page(page)
    except requests.exceptions.RequestException as exc:
        log.error("Failed to fetch Capitol Trades page %d (both JSON and HTML failed): %s", page, exc)
        return []
    html_trades = _parse_trades_page(html)
    if html_trades:
        log.debug("Capitol Trades page %d: fetched %d trades via HTML fallback", page, len(html_trades))
    else:
        log.warning(
            "Capitol Trades page %d: JSON endpoint unavailable AND HTML returned 0 trades "
            "(SPA rendering issue — data feed may be empty).", page
        )
    return html_trades
