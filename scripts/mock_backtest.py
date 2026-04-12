"""
Run freqtrade backtesting against the real user_data configuration while mocking
exchange network calls.

Default paths injected by this script:
  --config D:/_code/ft_userdata/user_data/config.json
  --user-data-dir D:/_code/ft_userdata/user_data
  --strategy-path D:/_code/ft_userdata/user_data/strategies

Backtest exports therefore default to the external ft_userdata tree, not to this
repository's local user_data directory.

Examples:
python scripts/mock_backtest.py --strategy Mar3Strategy --timeframe 1m --timerange 20220301-20220401 --cache none

Script-only options:
  --show-trades     Print the first N raw trades after the run.
  --trade-limit N   Maximum number of trades to print per strategy.
        --exchange-info   Optional exchangeInfo path. Defaults to future-exchangeInfo.json in futures mode.
"""

from __future__ import annotations

import argparse
import ccxt
import json
import sys
from contextlib import ExitStack
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

from freqtrade import constants
from freqtrade.configuration.configuration import Configuration
from freqtrade.enums import CandleType, RunMode
from freqtrade.exchange.exchange import Exchange
from freqtrade.exceptions import ConfigurationError, OperationalException
from freqtrade.optimize.backtesting import Backtesting
from freqtrade.util import fmt_coin, get_dry_run_wallet


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORK_ROOT = Path(r"D:\_code\ft_userdata")
DEFAULT_USER_DATA_DIR = DEFAULT_WORK_ROOT / "user_data"
DEFAULT_CONFIG = DEFAULT_USER_DATA_DIR / "config.json"
DEFAULT_STRATEGY_PATH = DEFAULT_USER_DATA_DIR / "strategies"
DEFAULT_SPOT_EXCHANGE_INFO = DEFAULT_WORK_ROOT / "exchangeInfo.json"
DEFAULT_FUTURES_EXCHANGE_INFO = DEFAULT_WORK_ROOT / "future-exchangeInfo.json"
EXMS = "freqtrade.exchange.exchange.Exchange"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mock_backtest",
        description="Run backtesting directly via the Backtesting API with mocked exchange metadata.",
    )
    parser.add_argument("--show-trades", action="store_true", help="Print a compact raw trade table after the run.")
    parser.add_argument("--trade-limit", type=int, default=10, help="Maximum number of trades to print with --show-trades.")
    parser.add_argument("--exchange-info", type=Path, default=None, help="Optional path to an exchangeInfo JSON file.")
    parser.add_argument("-v", "--verbose", dest="verbosity", action="count")
    parser.add_argument("--logfile", "--log-file", dest="logfile")
    parser.add_argument("-c", "--config", dest="config", action="append")
    parser.add_argument("--user-data-dir", dest="user_data_dir", default=str(DEFAULT_USER_DATA_DIR))
    parser.add_argument("--strategy-path", dest="strategy_path", default=str(DEFAULT_STRATEGY_PATH))
    parser.add_argument("-s", "--strategy", dest="strategy")
    parser.add_argument("--strategy-list", dest="strategy_list", nargs="+")
    parser.add_argument("-i", "--timeframe", dest="timeframe")
    parser.add_argument("--timeframe-detail", dest="timeframe_detail")
    parser.add_argument("--timerange", dest="timerange")
    parser.add_argument("--max-open-trades", dest="max_open_trades", type=int)
    parser.add_argument("--stake-amount", dest="stake_amount")
    parser.add_argument("--dry-run-wallet", "--starting-balance", dest="dry_run_wallet", type=float)
    parser.add_argument("--fee", dest="fee", type=float)
    parser.add_argument("-p", "--pairs", dest="pairs", nargs="+")
    parser.add_argument("-d", "--datadir", "--data-dir", dest="datadir")
    parser.add_argument("--data-format-ohlcv", dest="dataformat_ohlcv", choices=constants.AVAILABLE_DATAHANDLERS, default="feather")
    parser.add_argument("--data-format-trades", dest="dataformat_trades", choices=constants.AVAILABLE_DATAHANDLERS, default="feather")
    parser.add_argument("--export", dest="export", choices=constants.EXPORT_OPTIONS, default="trades")
    parser.add_argument("--backtest-directory", "--export-directory", dest="exportdirectory")
    parser.add_argument("--backtest-filename", "--export-filename", dest="exportfilename")
    parser.add_argument("--breakdown", dest="backtest_breakdown", nargs="+", choices=constants.BACKTEST_BREAKDOWNS)
    parser.add_argument("--cache", dest="backtest_cache", default=constants.BACKTEST_CACHE_DEFAULT, choices=constants.BACKTEST_CACHE_AGE)
    parser.add_argument("--notes", dest="backtest_notes")
    parser.add_argument("--enable-position-stacking", "--eps", dest="position_stacking", action="store_true")
    parser.add_argument("--no-color", dest="print_colorized", action="store_false")

    args = list(argv)
    if args and args[0] == "backtesting":
        args = args[1:]

    parsed_args = parser.parse_args(args)
    if not parsed_args.config:
        parsed_args.config = [str(DEFAULT_CONFIG)]
    return parsed_args


def namespace_to_configuration_args(parsed_args: argparse.Namespace) -> dict[str, Any]:
    config_args = {
        key: value
        for key, value in vars(parsed_args).items()
        if key not in {"show_trades", "trade_limit", "exchange_info"}
    }
    config_args["command"] = "backtesting"
    return config_args


def is_futures_mode(config: dict[str, Any]) -> bool:
    return str(config.get("trading_mode", "spot")) == "futures"


def pair_to_exchange_symbol(pair: str) -> str:
    if "/" not in pair:
        raise ValueError(f"Unsupported pair format: {pair!r}. Expected BASE/QUOTE.")
    base, quote_part = pair.split("/", 1)
    quote = quote_part.split(":", 1)[0]
    return f"{base}{quote}"


def load_exchange_info_subset(exchange_info_path: Path, pairs: list[str]) -> dict[str, dict[str, Any]]:
    if not exchange_info_path.is_file():
        return {}

    requested_symbols = {pair_to_exchange_symbol(pair) for pair in pairs}
    with exchange_info_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    symbols = payload.get("symbols", []) if isinstance(payload, dict) else []
    subset = {
        symbol_info["symbol"]: symbol_info
        for symbol_info in symbols
        if symbol_info.get("symbol") in requested_symbols
    }

    kept = ", ".join(sorted(subset)) if subset else "none"
    print(f"Loaded exchangeInfo symbols: {kept}")
    return subset


def prepare_backtest_run(
    parsed_args: argparse.Namespace,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    config = Configuration(namespace_to_configuration_args(parsed_args), RunMode.BACKTEST).get_config()

    if not config.get("strategy") and not config.get("strategy_list"):
        raise OperationalException("mock_backtest requires --strategy NAME or --strategy-list.")
    if not config.get("timeframe"):
        raise OperationalException("mock_backtest requires --timeframe TIMEFRAME.")

    wallet_size = get_dry_run_wallet(config) * config["tradable_balance_ratio"]
    if (
        config["stake_amount"] != constants.UNLIMITED_STAKE_AMOUNT
        and config["stake_amount"] > wallet_size
    ):
        wallet = fmt_coin(wallet_size, config["stake_currency"])
        stake = fmt_coin(config["stake_amount"], config["stake_currency"])
        raise ConfigurationError(
            f"Starting balance ({wallet}) is smaller than stake_amount {stake}. "
            "Wallet is calculated as `dry_run_wallet * tradable_balance_ratio`."
        )
    exchange_info_path = parsed_args.exchange_info or (
        DEFAULT_FUTURES_EXCHANGE_INFO if is_futures_mode(config) else DEFAULT_SPOT_EXCHANGE_INFO
    )
    pair_whitelist = config.get("exchange", {}).get("pair_whitelist", [])
    exchange_info_subset = load_exchange_info_subset(exchange_info_path, pair_whitelist)

    if not exchange_info_subset:
        raise OperationalException(
            "mock_backtest now requires exchangeInfo metadata so markets can be parsed via ccxt. "
            "Provide --exchange-info PATH and make sure it contains the whitelisted pairs."
        )

    missing_pairs = [
        pair for pair in pair_whitelist if pair_to_exchange_symbol(pair) not in exchange_info_subset
    ]
    if missing_pairs:
        raise OperationalException(
            "exchangeInfo is available but missing metadata for these pairs: "
            f"{', '.join(missing_pairs)}. Update exchangeInfo or reduce the whitelist."
        )

    if is_futures_mode(config):
        invalid_pairs = [
            pair
            for pair in pair_whitelist
            if not {
                "contractType",
                "marginAsset",
                "quantityPrecision",
                "pricePrecision",
            }.issubset(exchange_info_subset[pair_to_exchange_symbol(pair)].keys())
        ]
        if invalid_pairs:
            raise OperationalException(
                "Futures mode requires Binance USD-M futures exchangeInfo from /fapi/v1/exchangeInfo. "
                "The current --exchange-info file is missing futures fields for: "
                f"{', '.join(invalid_pairs)}."
            )

    return config, exchange_info_subset


def build_mock_exchange_apis(
    config: dict[str, Any],
    exchange_info_subset: dict[str, dict[str, Any]],
    timeframe_values: set[str],
) -> tuple[dict[str, dict[str, Any]], ccxt.Exchange, ccxt.Exchange]:
    exchange_name = str(config["exchange"]["name"])
    is_futures = is_futures_mode(config)

    try:
        exchange_module = import_module(f"freqtrade.exchange.{exchange_name}")
        ft_exchange_class = getattr(exchange_module, exchange_name.capitalize())
    except (ImportError, AttributeError):
        ft_exchange_class = Exchange

    ft_options = ft_exchange_class.combine_ft_has(include_futures=is_futures)
    exchange_class = getattr(ccxt, exchange_name, None)
    if exchange_class is None:
        raise OperationalException(f"ccxt exchange '{exchange_name}' is not available.")

    exchange_config: dict[str, Any] = {
        "options": {
            "crossMarginPairsData": [],
            "isolatedMarginPairsData": [],
        }
    }
    if is_futures:
        exchange_config["options"]["defaultType"] = ft_options.get("ccxt_futures_name", "swap")

    parser = exchange_class(exchange_config)
    markets: dict[str, dict[str, Any]] = {}
    for pair in config.get("exchange", {}).get("pair_whitelist", []):
        symbol_info = exchange_info_subset.get(pair_to_exchange_symbol(pair))
        if not symbol_info:
            raise OperationalException(f"Pair {pair} is missing from exchangeInfo metadata.")

        market = dict(parser.parse_market(symbol_info))
        market["symbol"] = pair
        market["taker"] = config.get("fee", market.get("taker", 0.0002))
        market["maker"] = config.get("fee", market.get("maker", 0.0002))

        limits = dict(market.get("limits") or {})
        leverage_limits = dict(limits.get("leverage") or {})
        leverage_limits["min"] = leverage_limits.get("min") or 1
        leverage_limits["max"] = leverage_limits.get("max") or (100 if is_futures else 5)
        limits["leverage"] = leverage_limits
        market["limits"] = limits

        if is_futures:
            market.setdefault("contractSize", 1)
        else:
            market["lot"] = market.get("precision", {}).get("amount")

        markets[pair] = market

    timeframes = {timeframe: timeframe for timeframe in sorted(timeframe_values)}
    apis: list[ccxt.Exchange] = []
    for _ in range(2):
        api = exchange_class(dict(exchange_config))
        api.timeframes = timeframes
        api.options.setdefault("timeframes", {})
        api.options["timeframes"].setdefault("spot", timeframes)
        api.options["timeframes"].setdefault("swap", timeframes)
        api.set_markets(markets)
        api.close = lambda: None
        apis.append(api)

    return markets, apis[0], apis[1]


def mock_exchange_context(
    config: dict[str, Any], exchange_info_subset: dict[str, dict[str, Any]]
) -> ExitStack:
    timeframe_values = {"1m", "5m", "15m", "1h", "4h", "8h", "1d", str(config["timeframe"])}
    if config.get("timeframe_detail"):
        timeframe_values.add(str(config["timeframe_detail"]))
    markets, sync_api, async_api = build_mock_exchange_apis(
        config, exchange_info_subset, timeframe_values
    )
    ccxt_inits = iter([sync_api, async_api])
    stack = ExitStack()

    stack.enter_context(
        patch(
            f"{EXMS}._init_ccxt",
            side_effect=lambda exchange_conf, sync, ccxt_kwargs: next(ccxt_inits),
        )
    )
    stack.enter_context(patch(f"{EXMS}._load_async_markets", return_value=None))

    if is_futures_mode(config):
        stack.enter_context(
            patch(
                f"{EXMS}.get_max_leverage",
                side_effect=lambda pair, stake_amount=None: markets[pair]["limits"]["leverage"]["max"],
            )
        )
        stack.enter_context(
            patch(f"{EXMS}.get_maintenance_ratio_and_amt", return_value=(0.01, 0.0))
        )

    return stack


def print_trade_tables(backtesting: Backtesting, trade_limit: int) -> None:
    if not backtesting.all_bt_content:
        print("Raw trade output is unavailable because the result was reused from cache.")
        print("Rerun with --cache none if you want to print raw trades.")
        return

    for strategy_name, content in backtesting.all_bt_content.items():
        results = content["results"]
        print()
        print(f"Strategy: {strategy_name}")
        print(f"Trades: {len(results)}")
        if results.empty:
            continue
        columns = [
            "pair",
            "open_date",
            "close_date",
            "profit_ratio",
            "profit_abs",
            "exit_reason",
        ]
        available_columns = [column for column in columns if column in results.columns]
        print(results.loc[:, available_columns].head(trade_limit).to_string(index=False))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parsed_args = parse_args(argv)
    config, exchange_info_subset = prepare_backtest_run(parsed_args)

    with mock_exchange_context(config, exchange_info_subset):
        backtesting = Backtesting(config)
        try:
            backtesting.start()
            if parsed_args.show_trades:
                print_trade_tables(backtesting, parsed_args.trade_limit)
        finally:
            Backtesting.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())