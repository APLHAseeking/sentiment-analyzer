import os
from dotenv import load_dotenv

load_dotenv()

def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val

ANTHROPIC_API_KEY: str = _require("ANTHROPIC_API_KEY")
ALPACA_API_KEY: str = _require("ALPACA_API_KEY")
ALPACA_SECRET_KEY: str = _require("ALPACA_SECRET_KEY")
ALPACA_BASE_URL: str = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
PROPUBLICA_API_KEY: str = _require("PROPUBLICA_API_KEY")
DB_PATH: str = os.environ.get("DB_PATH", "trading.db")
FINCEPT_SCRIPTS_PATH: str = os.environ.get(
    "FINCEPT_SCRIPTS_PATH",
    "/Users/thomasvromen/Documents/FinceptTerminal/fincept-qt/scripts/Analytics",
)
