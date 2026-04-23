from unittest.mock import MagicMock
from bot.broker import AlpacaBroker

def test_get_cash(mocker):
    mock_api = MagicMock()
    mock_api.get_account.return_value = MagicMock(cash="50000.00")
    mocker.patch("bot.broker._get_api", return_value=mock_api)
    broker = AlpacaBroker()
    assert broker.get_cash() == 50_000.0

def test_get_positions(mocker):
    mock_pos = MagicMock()
    mock_pos.symbol = "AAPL"
    mock_pos.qty = "10"
    mock_pos.current_price = "150.00"
    mock_pos.avg_entry_price = "140.00"
    mock_api = MagicMock()
    mock_api.get_all_positions.return_value = [mock_pos]
    mocker.patch("bot.broker._get_api", return_value=mock_api)
    broker = AlpacaBroker()
    positions = broker.get_positions()
    assert positions == [{
        "ticker": "AAPL", "qty": 10.0,
        "current_price": 150.0, "avg_entry_price": 140.0,
    }]

def test_place_order_buy(mocker):
    mock_api = MagicMock()
    mocker.patch("bot.broker._get_api", return_value=mock_api)
    broker = AlpacaBroker()
    broker.place_order(ticker="AAPL", side="buy", qty=10.0)
    mock_api.submit_order.assert_called_once()
    order = mock_api.submit_order.call_args[0][0]
    from alpaca.trading.enums import OrderSide
    assert order.symbol == "AAPL"
    assert float(order.qty) == 10.0
    assert order.side == OrderSide.BUY

def test_place_order_sell(mocker):
    mock_api = MagicMock()
    mocker.patch("bot.broker._get_api", return_value=mock_api)
    broker = AlpacaBroker()
    broker.place_order(ticker="AAPL", side="sell", qty=5.0)
    order = mock_api.submit_order.call_args[0][0]
    from alpaca.trading.enums import OrderSide
    assert order.side == OrderSide.SELL
