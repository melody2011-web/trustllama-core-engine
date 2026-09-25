"""Dedicated live-capable Binance-Peg ETH/USDT six-line grid launcher."""
from __future__ import annotations
import logging
import os
from dataclasses import replace
from logging.handlers import RotatingFileHandler
from pathlib import Path
from bot.isolated_grid import isolated_settings
from bot.live_runner import LivePriceStore, LiveTradingLoop

GRID_BOT_5_PRICE_HISTORY: list[object] = []
GRID_BOT_5_ERRORS: list[str] = []
GRID_BOT_5_LOGGER = logging.getLogger("grid_bot_5")
GRID_BOT_5_STATE_PATH = Path.home() / ".local/state/bnb-defi-bot/grid_bot_5/state.json"
GRID_BOT_5_CONFIG_PATH = Path.home() / ".local/state/bnb-defi-bot/grid_bot_5/config.json"
GRID_BOT_5_LOG_PATH = Path.home() / ".local/state/bnb-defi-bot/grid_bot_5/grid_bot_5.log"
GRID_BOT_5_PRICE_PATH = Path.home() / ".local/state/bnb-defi-bot/grid_bot_5/live-prices.json"

class _GridBot5PriceStore(LivePriceStore):
    def record_price(self, symbol, price, limit):
        GRID_BOT_5_PRICE_HISTORY.append((symbol, str(price)))
        del GRID_BOT_5_PRICE_HISTORY[:-100]
        return super().record_price(symbol, price, limit)

def _configure_logging() -> None:
    GRID_BOT_5_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(GRID_BOT_5_LOG_PATH, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    for name in ("grid_bot_5", "defi-bot.live"):
        log = logging.getLogger(name); log.addHandler(handler); log.setLevel(logging.INFO); log.propagate = False

def main() -> None:
    if os.environ.get("ENABLE_LIVE_TRADING", "").strip().lower() != "true":
        raise RuntimeError("grid_bot_5 refuses to start unless ENABLE_LIVE_TRADING=true.")
    def load():
        settings = isolated_settings("grid_bot_5", GRID_BOT_5_CONFIG_PATH)
        return replace(settings, strategy_state_file=str(GRID_BOT_5_STATE_PATH), live_price_history_file=str(GRID_BOT_5_PRICE_PATH))
    settings = load()
    _configure_logging()
    try:
        LiveTradingLoop(settings, prices=_GridBot5PriceStore(settings.live_price_history_file, ("ETH",)), settings_provider=load).run()
    except Exception as error:
        GRID_BOT_5_ERRORS.append(type(error).__name__); del GRID_BOT_5_ERRORS[:-100]
        GRID_BOT_5_LOGGER.error("grid_bot_5 stopped: %s", type(error).__name__)
        raise

if __name__ == "__main__":
    main()