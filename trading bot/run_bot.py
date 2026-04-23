import logging
from bot.db import init_db
from bot.universe import refresh_universe
from bot.broker import AlpacaBroker
from bot.portfolio import Portfolio
from bot.scheduler import start

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

if __name__ == "__main__":
    init_db()
    refresh_universe()
    broker = AlpacaBroker()
    portfolio = Portfolio(broker=broker)
    start(portfolio)
