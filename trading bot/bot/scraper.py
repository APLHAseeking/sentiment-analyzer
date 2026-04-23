import requests
from datetime import datetime, UTC
from bs4 import BeautifulSoup
from bot.db import get_existing_ids, insert_disclosures

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; congress-bot/1.0; research-only)"}
TRADES_URL = "https://capitoltrades.com/trades"

def _parse_trades_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table.q-table tbody tr")
    trades = []
    for row in rows:
        cells = row.select("td")
        if len(cells) < 7:
            continue
        trade_id = row.get("data-id", "").strip()
        ticker = cells[2].get_text(strip=True)
        if not trade_id or not ticker:
            continue
        trades.append({
            "id": trade_id,
            "politician": cells[0].get_text(strip=True),
            "ticker": ticker,
            "transaction_type": cells[3].get_text(strip=True).lower(),
            "transaction_date": cells[4].get_text(strip=True),
            "disclosure_date": cells[5].get_text(strip=True),
            "amount_range": cells[6].get_text(strip=True),
            "scraped_at": datetime.now(UTC).isoformat(),
        })
    return trades

def _fetch_page(page: int) -> str:
    resp = requests.get(
        TRADES_URL,
        params={"page": page, "pageSize": 100},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.text

def run_scraper(max_pages: int = 3) -> list[dict]:
    """Fetch new disclosures from Capitol Trades and persist them. Returns new records."""
    existing = get_existing_ids()
    new_trades: list[dict] = []
    for page in range(1, max_pages + 1):
        html = _fetch_page(page)
        trades = _parse_trades_page(html)
        if not trades:
            break
        fresh = [t for t in trades if t["id"] not in existing]
        new_trades.extend(fresh)
        if len(fresh) < len(trades):
            break  # hit the already-seen boundary
    if new_trades:
        insert_disclosures(new_trades)
    return new_trades
