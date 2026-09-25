"""Dedicated SOL/USDT live WebSocket scalper."""
from __future__ import annotations
import logging
import signal
from dataclasses import replace
from logging.handlers import RotatingFileHandler
from bot.config import Settings
from bot.live_scalper import LiveScalper, ScalperPaths
BOT, MARKET_SYMBOL, TRADE_SYMBOL = "scalper_bot_9", "SOLUSDT", "SOL"
PATHS = ScalperPaths.for_bot(BOT)
LOGGER = logging.getLogger(BOT)
def main() -> None:
    settings = replace(Settings.from_environment(isolated_scalper_profile=BOT), strategy_state_file=str(PATHS.trader_state))
    PATHS.root.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(PATHS.log, maxBytes=1_000_000, backupCount=3)
    LOGGER.addHandler(handler); LOGGER.setLevel(logging.INFO); LOGGER.propagate = False
    scalper = LiveScalper(settings, paths=PATHS, market_symbol=MARKET_SYMBOL, trade_symbol=TRADE_SYMBOL)
    signal.signal(signal.SIGINT, lambda *_: scalper.stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: scalper.stop_event.set())
    scalper.run()
if __name__ == "__main__":
    main()