"""Corporate-insider signal source: SEC EDGAR Form 4 open-market buys.

Mirrors the congressional scraper (`bot/scraper.py`) but reads SEC EDGAR Form 4
filings (statements of changes in beneficial ownership). Only open-market
*purchases* (transaction code ``P``, acquired) are emitted — option grants,
gifts, automatic plan sells, etc. are ignored. Data is free; the only cost is
the bounded LLM scoring of the few candidates that survive filtering (capped via
`InsiderConfig.max_per_day`).

SEC fair-access policy requires a descriptive User-Agent with a contact email and
limits clients to ~10 requests/second; both are honoured here.
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, UTC

import requests

from bot.db import get_existing_insider_ids, insert_insider_disclosures
from monitoring.logger import EventType, emit_event
from system.config import settings

log = logging.getLogger(__name__)

# Recent Form 4 filings as an Atom feed (most recent first).
_GETCURRENT_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TICKER_RE = re.compile(r"^[A-Z]{1,5}([.\-][A-Z])?$")
_ACCESSION_RE = re.compile(r"accession[_-]?number=([\d-]+)", re.IGNORECASE)
_ACCESSION_PATH_RE = re.compile(r"/(\d{10}-\d{2}-\d{6})", re.IGNORECASE)
_INTER_REQUEST_SLEEP = 0.12  # ~8 req/s, under SEC's 10 req/s fair-access ceiling


def _headers() -> dict[str, str]:
    return {"User-Agent": settings.insider.sec_user_agent, "Accept-Encoding": "gzip, deflate"}


def _normalize_ticker(raw: str) -> str:
    """SEC issuerTradingSymbol uses dots for class shares (BRK.B); yfinance wants hyphens."""
    return raw.replace(".", "-").upper().strip()


def _text(node: ET.Element | None) -> str:
    """Return stripped text from an element (or its <value> child), '' if absent."""
    if node is None:
        return ""
    value = node.find("value")
    target = value if value is not None else node
    return (target.text or "").strip()


def _classify_title(relationship: ET.Element | None) -> str:
    """Map a reportingOwnerRelationship element to a short role string."""
    if relationship is None:
        return "Insider"
    officer_title = _text(relationship.find("officerTitle"))
    if officer_title:
        return officer_title
    if _text(relationship.find("isOfficer")) in ("1", "true"):
        return "Officer"
    if _text(relationship.find("isDirector")) in ("1", "true"):
        return "Director"
    if _text(relationship.find("isTenPercentOwner")) in ("1", "true"):
        return "10% Owner"
    return "Insider"


def parse_form4_xml(xml_text: str, accession: str, filing_date: str) -> list[dict]:
    """Parse a Form 4 ownership XML into normalized open-market-purchase dicts.

    Pure function (no network/DB) so it is fully unit-testable. Emits one dict per
    non-derivative transaction with code ``P`` (open-market purchase) that was an
    acquisition. Multiple purchases in one filing get distinct ids (``acc:idx``).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("Form 4 %s: XML parse error: %s", accession, exc)
        return []

    issuer = root.find("issuer")
    ticker = _normalize_ticker(_text(issuer.find("issuerTradingSymbol")) if issuer is not None else "")
    if not ticker or not _TICKER_RE.match(ticker):
        return []

    owner = root.find("reportingOwner")
    insider_name = _text(owner.find("reportingOwnerId/rptOwnerName")) if owner is not None else ""
    title = _classify_title(owner.find("reportingOwnerRelationship") if owner is not None else None)

    now_iso = datetime.now(UTC).isoformat()
    results: list[dict] = []
    for idx, tx in enumerate(root.findall("nonDerivativeTable/nonDerivativeTransaction")):
        code = _text(tx.find("transactionCoding/transactionCode"))
        if code != "P":  # P = open-market purchase
            continue
        amounts = tx.find("transactionAmounts")
        if amounts is None:
            continue
        if _text(amounts.find("transactionAcquiredDisposedCode")) != "A":  # A = acquired
            continue
        try:
            shares = float(_text(amounts.find("transactionShares")) or 0)
            price = float(_text(amounts.find("transactionPricePerShare")) or 0)
        except ValueError:
            continue
        amount_usd = shares * price
        if amount_usd <= 0:
            continue
        tx_date = _text(tx.find("transactionDate"))
        if not _ISO_DATE_RE.match(tx_date) or not _ISO_DATE_RE.match(filing_date):
            continue
        results.append({
            "id": f"{accession}:{idx}",
            "insider_name": insider_name or "Unknown",
            "ticker": ticker,
            "title": title,
            "transaction_type": "buy",
            "transaction_date": tx_date,
            "disclosure_date": filing_date,
            "amount_usd": amount_usd,
            "scraped_at": now_iso,
        })
    return results


def _fetch_recent_form4_index(count: int) -> list[dict]:
    """Fetch the most recent Form 4 filings via the EDGAR getcurrent Atom feed.

    Returns a list of {"accession", "cik", "filing_date"} dicts. Network errors
    return [] (caller treats an empty page-1 result as a dead feed)."""
    try:
        resp = requests.get(
            _GETCURRENT_URL,
            params={
                "action": "getcurrent", "type": "4", "owner": "include",
                "count": count, "output": "atom",
            },
            headers=_headers(), timeout=30,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        log.error("SEC getcurrent Form 4 feed fetch failed: %s", exc)
        return []

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        log.error("SEC getcurrent feed XML parse error: %s", exc)
        return []

    # Atom namespace-agnostic: strip namespaces by matching local tag names.
    def _local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    entries: list[dict] = []
    for entry in root.iter():
        if _local(entry.tag) != "entry":
            continue
        href = ""
        filing_date = ""
        for child in entry:
            lt = _local(child.tag)
            if lt == "link":
                href = child.get("href", "") or href
            elif lt == "updated" and not filing_date:
                filing_date = (child.text or "")[:10]
        m = _ACCESSION_RE.search(href) or _ACCESSION_PATH_RE.search(href)
        accession = m.group(1).replace("_", "-") if m else ""
        if accession and _ISO_DATE_RE.match(filing_date):
            entries.append({"accession": accession, "href": href, "filing_date": filing_date})
    return entries


def _fetch_form4_xml(accession: str, href: str) -> str | None:
    """Fetch the Form 4 ownership XML document for a filing index URL.

    EDGAR exposes a per-filing JSON directory listing at the index path; the XML
    document is the entry whose name ends in .xml. Returns None on any failure."""
    base = href.rsplit("/", 1)[0] if href.endswith((".htm", ".html")) else href.rstrip("/")
    try:
        listing = requests.get(f"{base}/", headers=_headers(), timeout=30)
        listing.raise_for_status()
        # Find the first .xml document referenced in the directory listing.
        m = re.search(r'href="([^"]+\.xml)"', listing.text)
        if not m:
            m = re.search(r'([A-Za-z0-9_\-]+\.xml)', listing.text)
            xml_name = m.group(1) if m else None
            xml_url = f"{base}/{xml_name}" if xml_name else None
        else:
            doc = m.group(1)
            xml_url = doc if doc.startswith("http") else f"{base}/{doc.lstrip('/')}"
        if not xml_url:
            return None
        xml_resp = requests.get(xml_url, headers=_headers(), timeout=30)
        xml_resp.raise_for_status()
        return xml_resp.text
    except requests.exceptions.RequestException as exc:
        log.debug("Form 4 %s: XML document fetch failed: %s", accession, exc)
        return None


def run_insider_scraper() -> list[dict]:
    """Scrape recent SEC Form 4 open-market purchases. Returns new (deduped) buys.

    Persists new rows to `insider_disclosures`. Fires a DEAD_FEED alert if the feed
    yields nothing on the first page (mirrors the congressional scraper)."""
    cfg = settings.insider
    if not cfg.enabled:
        return []

    index = _fetch_recent_form4_index(cfg.max_filings_per_run)
    if not index:
        emit_event(log, EventType.DEAD_FEED,
                   "SEC Form 4 feed returned 0 filings — insider signal pipeline dead",
                   alert=True)
        return []

    existing = get_existing_insider_ids()
    new_buys: list[dict] = []
    for entry in index[:cfg.max_filings_per_run]:
        xml_text = _fetch_form4_xml(entry["accession"], entry.get("href", ""))
        time.sleep(_INTER_REQUEST_SLEEP)
        if not xml_text:
            continue
        for buy in parse_form4_xml(xml_text, entry["accession"], entry["filing_date"]):
            if buy["id"] not in existing:
                new_buys.append(buy)

    if new_buys:
        insert_insider_disclosures(new_buys)
    log.info("Insider Form 4: %d filings scanned, %d new open-market buys", len(index), len(new_buys))
    return new_buys
