"""Shared, timeout-bound yfinance session.

yf.Ticker(ticker) with no session kwarg creates a brand-new curl_cffi
session per call, never closed, and neither that per-call session nor
yfinance's own request path sets a network timeout — a single stalled
request can block the caller forever. On the live bot's single-thread
APScheduler executor, that blocks every subsequent scheduled job too:
first found as 503 leaked CLOSE_WAIT sockets hitting the OS fd limit
(commit fba2143, screener/factor_scorer.py), then found again days later
as an indefinite scheduler-wide hang with only ~4 sockets open this
time — the leak was fixed, the missing timeout wasn't.

Two entry points:
- make_shared_yf_session(): a fresh session per call, for callers that
  fetch a batch and explicitly close it when done (see
  screener/factor_scorer.py::_fetch_all_infos).
- get_shared_yf_session(): a lazily-created, process-lifetime singleton
  for scattered one-off call sites with no natural batch boundary to
  close on (reused the same way `requests.Session` is meant to be — for
  connection pooling across the process, not recreated per call).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

YF_TIMEOUT_SECONDS = 10


def make_shared_yf_session():
    """One shared session, matching yfinance's own default construction
    (yfinance.base.TickerBase: `requests.Session(impersonate="chrome")`,
    where yfinance internally aliases `requests` to `curl_cffi.requests`
    for Yahoo's bot-detection bypass) plus an explicit timeout it doesn't
    set. Falls back to yfinance's own per-call default (session=None) if
    curl_cffi ever becomes unimportable, rather than crashing.
    """
    try:
        import curl_cffi.requests as curl_requests
        return curl_requests.Session(impersonate="chrome", timeout=YF_TIMEOUT_SECONDS)
    except Exception as exc:
        log.warning("Could not create shared yfinance session (%s) — falling back "
                    "to per-call sessions (yfinance default, no timeout)", exc)
        return None


_singleton = None


def get_shared_yf_session():
    """Lazily-created, process-lifetime singleton session for scattered
    one-off yfinance call sites (see module docstring)."""
    global _singleton
    if _singleton is None:
        _singleton = make_shared_yf_session()
    return _singleton
