import os
from dotenv import load_dotenv

from system.paths import resolve

load_dotenv()

def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val

OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")
# Alpaca keys only required when AlpacaBroker is used (not simulated mode)
ALPACA_API_KEY: str = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY: str = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL: str = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
PROPUBLICA_API_KEY: str = os.environ.get("PROPUBLICA_API_KEY", "")
DB_PATH: str = resolve(os.environ.get("DB_PATH", "trading.db"))
FINCEPT_SCRIPTS_PATH: str = os.environ.get(
    "FINCEPT_SCRIPTS_PATH",
    "/Users/thomasvromen/Documents/FinceptTerminal/fincept-qt/scripts/Analytics",
)
