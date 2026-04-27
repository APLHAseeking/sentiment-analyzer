import pytest
from bot.analytics import compute_performance_report, PerformanceReport


def _insert_closed(db, ticker, entry, exit_, shares, entry_date, exit_date, signal_id):
    db.log_closed_position(
        ticker=ticker, entry_price=entry, exit_price=exit_,
        shares=shares, entry_date=entry_date, exit_date=exit_date,
        exit_reason="test", signal_id=signal_id,
    )


def _make_disc(db, disc_id: str, ticker: str) -> int:
    db.insert_disclosures([{
        "id": disc_id, "politician": "J", "ticker": ticker,
        "transaction_date": "2026-04-01", "disclosure_date": "2026-04-05",
        "transaction_type": "purchase", "amount_range": "$50,001 - $100,000",
        "scraped_at": "2026-04-26T08:00:00",
    }])
    return db.insert_signal(disc_id, ticker, 8, 5.0, "Good", [])


def test_performance_report_win_rate(db):
    sid_a = _make_disc(db, "pr-001", "AAPL")
    sid_b = _make_disc(db, "pr-002", "MSFT")
    sid_c = _make_disc(db, "pr-003", "GOOG")
    _insert_closed(db, "AAPL", 100, 120, 10, "2026-04-01", "2026-04-15", sid_a)  # +200 win
    _insert_closed(db, "MSFT", 200, 240, 5, "2026-04-01", "2026-04-20", sid_b)   # +200 win
    _insert_closed(db, "GOOG", 150, 135, 8, "2026-04-02", "2026-04-18", sid_c)   # -120 loss
    report = compute_performance_report()
    assert isinstance(report, PerformanceReport)
    assert report.total_trades == 3
    assert report.wins == 2
    assert report.losses == 1
    assert abs(report.win_rate - 2/3) < 0.01


def test_performance_report_avg_hold_days(db):
    sid_a = _make_disc(db, "ph-001", "XOM")
    sid_b = _make_disc(db, "ph-002", "CVX")
    _insert_closed(db, "XOM", 100, 115, 5, "2026-04-01", "2026-04-11", sid_a)   # 10 days
    _insert_closed(db, "CVX", 80, 90, 5, "2026-04-01", "2026-04-21", sid_b)     # 20 days
    report = compute_performance_report()
    assert abs(report.avg_hold_days - 15.0) < 0.1


def test_performance_report_total_pnl(db):
    sid_a = _make_disc(db, "pp-001", "LMT")
    sid_b = _make_disc(db, "pp-002", "RTX")
    _insert_closed(db, "LMT", 100, 110, 10, "2026-04-01", "2026-04-15", sid_a)  # +100
    _insert_closed(db, "RTX", 200, 190, 5, "2026-04-01", "2026-04-18", sid_b)   # -50
    report = compute_performance_report()
    assert abs(report.total_realized_pnl - 50.0) < 0.01


def test_empty_performance_report(db):
    report = compute_performance_report()
    assert report.total_trades == 0
    assert report.win_rate == 0.0
    assert report.total_realized_pnl == 0.0
    assert report.avg_hold_days == 0.0


def test_performance_report_best_and_worst(db):
    sid_a = _make_disc(db, "bw-001", "NVDA")
    sid_b = _make_disc(db, "bw-002", "META")
    _insert_closed(db, "NVDA", 100, 150, 10, "2026-04-01", "2026-04-15", sid_a)  # +500
    _insert_closed(db, "META", 300, 270, 5, "2026-04-01", "2026-04-20", sid_b)   # -150
    report = compute_performance_report()
    assert abs(report.best_trade_pnl - 500.0) < 0.01
    assert abs(report.worst_trade_pnl - (-150.0)) < 0.01
