from unittest.mock import patch

from market_data.yf_session import (
    make_shared_yf_session,
    get_shared_yf_session,
    YF_TIMEOUT_SECONDS,
)
import market_data.yf_session as yf_session_mod


def test_make_shared_yf_session_sets_timeout():
    """The whole point of this module: yf.Ticker()'s own session (and the
    yfinance default it replaces) never enforces a timeout, so a stalled
    request can hang the caller forever. A fresh session must carry one."""
    session = make_shared_yf_session()
    assert session is not None
    assert session.timeout == YF_TIMEOUT_SECONDS
    session.close()


def test_make_shared_yf_session_falls_back_to_none_on_import_failure():
    with patch.dict("sys.modules", {"curl_cffi.requests": None}):
        result = make_shared_yf_session()
    assert result is None


def test_get_shared_yf_session_is_a_singleton():
    """Scattered one-off call sites (main_loop.py, researcher.py, etc.) reuse
    one session across the process instead of leaking a fresh one per call."""
    yf_session_mod._singleton = None
    try:
        first = get_shared_yf_session()
        second = get_shared_yf_session()
        assert first is second
    finally:
        yf_session_mod._singleton = None
