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
        try:
            html = _fetch_page(page)
        except requests.exceptions.RequestException as exc:
            log.error("Failed to fetch Capitol Trades page %d after retries: %s", page, exc)
            break
        trades = _parse_trades_page(html)
        if not trades:
            if page == 1:
                log.error(
                    "No trades parsed from Capitol Trades page 1 — HTML structure may "
                    "have changed. Signal pipeline is receiving zero inputs. Manual "
                    "inspection of %s required.", TRADES_URL
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
