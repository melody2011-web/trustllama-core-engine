from __future__ import annotations

import argparse
import json
import logging
from decimal import Decimal, InvalidOperation

from .config import ConfigurationError, Settings
from .trader import (
    BscPreflight,
    PancakeSwapTrader,
    StrategyStateStore,
    TraderError,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("defi-bot")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute a guarded PancakeSwap V3 swap on BNB Smart Chain."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--action", choices=("buy", "sell", "quote"))
    mode.add_argument(
        "--strategy",
        action="store_true",
        help="Evaluate the configured EMA/RSI strategy and execute at most one order.",
    )
    mode.add_argument(
        "--reconcile-strategy",
        action="store_true",
        help="Confirm the receipt of a previously broadcast strategy order.",
    )
    mode.add_argument(
        "--reconcile-manual",
        action="store_true",
        help="Resolve the receipt of a previously broadcast manual order.",
    )
    mode.add_argument(
        "--inspect-pending",
        action="store_true",
        help="Read the saved pending trade before receipt reconciliation; never changes it.",
    )
    mode.add_argument(
        "--preflight",
        action="store_true",
        help=(
            "Run the active Grid profile's read-only BSC, funding, journal, "
            "and direct V3 quote checks."
        ),
    )
    parser.add_argument("--symbol", choices=("XRP", "BTC"))
    parser.add_argument(
        "--quote-symbol",
        choices=("XRP", "BTC"),
        help=(
            "Output token for a read-only token-to-token quote. "
            "For example, XRP --quote-symbol BTC evaluates XRP -> USDT -> BTC."
        ),
    )
    parser.add_argument(
        "--amount",
        help="Human-readable amount: USDT for buys, token units for sells.",
    )
    parser.add_argument(
        "--prices",
        help=(
            "Comma-separated price history for --strategy, newest price last. "
            "The strategy needs at least 22 prices with the default settings."
        ),
    )
    return parser

def _prices(raw: str | None) -> list[Decimal]:
    if not raw:
        raise TraderError("--prices is required when --strategy is used.")
    try:
        prices = [Decimal(value.strip()) for value in raw.split(",")]
    except InvalidOperation as error:
        raise TraderError("--prices must be a comma-separated list of decimals.") from error
    if not prices or any(not price.is_finite() or price <= 0 for price in prices):
        raise TraderError("--prices must contain positive decimal values.")
    return prices
def main() -> int:
    args = _parser().parse_args()
    try:
        _validate_manual_args(args)
        if args.preflight:
            # The preflight needs the public wallet address for balance reads,
            # but intentionally never loads WALLET_PRIVATE_KEY or SESSION_SECRET.
            settings = Settings.from_environment(
                require_wallet=True,
                require_private_key=False,
            )
            report = BscPreflight(settings).run()
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0 if report["ok"] else 1

        settings = Settings.from_environment()

        if args.inspect_pending:
            pending = StrategyStateStore(settings.strategy_state_file).pending_trade()
            if pending is None:
                logger.info("No pending manual or strategy transaction.")
            else:
                logger.info(
                    "Pending %s trade: status=%s action=%s asset=%s amount=%s tx=%s phase=%s",
                    pending.kind,
                    "broadcast-pending" if pending.tx_hash else "claimed-before-broadcast",
                    pending.action,
                    pending.symbol,
                    pending.amount,
                    pending.tx_hash or "unavailable",
                    pending.phase or "unavailable",
                )
            return 0
        trader = PancakeSwapTrader(settings)
        if args.reconcile_strategy:
            result = trader.reconcile_strategy()
            if result is None:
                logger.info("No pending strategy order has a confirmed receipt yet.")
                return 0
            logger.info(
                "Reconciled confirmed strategy %s %s trade: tx=%s block=%s",
                result.action,
                result.symbol,
                result.tx_hash,
                result.block_number,
            )
            return 0
        if args.reconcile_manual:
            result = trader.reconcile_manual()
            status = getattr(trader, "_last_manual_reconciliation_status", None)
            if result is not None:
                logger.info(
                    "Reconciled confirmed manual %s %s trade: tx=%s block=%s",
                    result.action,
                    result.symbol,
                    result.tx_hash,
                    result.block_number,
                )
            elif status == "reverted":
                logger.info(
                    "Reconciled reverted manual transaction; it is safe to submit a new trade."
                )
            elif status == "approval_confirmed":
                logger.info(
                    "Reconciled confirmed approval; the manual swap was not broadcast "
                    "and can be submitted again."
                )
            else:
                logger.info("No pending manual order has a confirmed receipt yet.")
            return 0
        if args.strategy:
            result = trader.execute_strategy(args.symbol, _prices(args.prices))
            if result is None:
                logger.info("Strategy produced no order for %s.", args.symbol)
                return 0
        elif args.action == "quote":
            if args.quote_symbol:
                logger.info(
                    "Quote: %s",
                    trader.quote_pair(args.symbol, args.quote_symbol, args.amount),
                )
            else:
                logger.info("Quote: %s", trader.quote("buy", args.symbol, args.amount))
            return 0
        else:
            result = trader.execute(args.action, args.symbol, args.amount)

        logger.info(
            "Confirmed live %s %s trade: input=%s output=%s tx=%s block=%s",
            result.action,
            result.symbol,
            result.amount_in,
            result.amount_out,
            result.tx_hash,
            result.block_number,
        )
        return 0
    except (ConfigurationError, TraderError) as error:
        logger.error("%s", error)
        return 1
    except Exception as error:
        if args.preflight:
            # Provider exceptions sometimes embed credential-bearing RPC URLs.
            # Report only the exception category for this read-only diagnostic.
            logger.error(
                "BSC preflight failed safely (%s); no transaction was attempted.",
                type(error).__name__,
            )
            return 1
        raise


def _validate_manual_args(args: argparse.Namespace) -> None:
    if args.preflight:
        if (
            args.symbol
            or args.amount
            or args.prices
            or args.quote_symbol
        ):
            raise TraderError(
                "--symbol, --amount, --prices, and --quote-symbol are not used with --preflight."
            )
        return
    if args.inspect_pending or args.reconcile_strategy or args.reconcile_manual:
        if args.amount or args.prices or args.quote_symbol:
            raise TraderError(
                "--amount, --prices, and --quote-symbol are not used with pending inspection or receipt reconciliation."
            )
        return
    if args.strategy:
        if args.amount or args.quote_symbol:
            raise TraderError("--amount and --quote-symbol are not used with --strategy.")
        if not args.symbol:
            raise TraderError("--symbol is required with --strategy.")
        return
    if not args.symbol:
        raise TraderError("--symbol is required with --action.")
    if args.action != "quote" and not args.amount:
        raise TraderError("--amount is required for buy and sell.")
    if args.action == "quote" and not args.amount:
        raise TraderError("--amount is required for quote.")
    if args.quote_symbol and args.action != "quote":
        raise TraderError("--quote-symbol is only valid with --action quote.")
    if args.quote_symbol == args.symbol:
        raise TraderError("--quote-symbol must be different from --symbol.")
    if args.action and args.prices:
        raise TraderError("--prices can only be used with --strategy.")


if __name__ == "__main__":
    raise SystemExit(main())
