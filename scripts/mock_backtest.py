"""
Run freqtrade backtesting against the real user_data configuration while mocking
exchange network calls.

Default paths injected by this script:
  --config D:/_code/ft_userdata/user_data/config.json
  --user-data-dir D:/_code/ft_userdata/user_data
  --strategy-path D:/_code/ft_userdata/user_data/strategies

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

from freqtrade.commands.arguments import Arguments
from freqtrade.commands.optimize_commands import setup_optimize_configuration
from freqtrade.enums import CandleType, RunMode
from freqtrade.exchange.exchange import Exchange
from freqtrade.exceptions import OperationalException
from freqtrade.optimize.backtesting import Backtesting


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORK_ROOT = Path(r"D:\_code\ft_userdata")
DEFAULT_USER_DATA_DIR = DEFAULT_WORK_ROOT / "user_data"
DEFAULT_CONFIG = DEFAULT_USER_DATA_DIR / "config.json"
DEFAULT_STRATEGY_PATH = DEFAULT_USER_DATA_DIR / "strategies"
DEFAULT_SPOT_EXCHANGE_INFO = DEFAULT_WORK_ROOT / "exchangeInfo.json"
DEFAULT_FUTURES_EXCHANGE_INFO = DEFAULT_WORK_ROOT / "future-exchangeInfo.json"
EXMS = "freqtrade.exchange.exchange.Exchange"


def parse_script_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--show-trades",
        action="store_true",
        help="Print a compact raw trade table after the run.",
    )
    parser.add_argument(
        "--trade-limit",
        type=int,
        default=10,
        help="Maximum number of trades to print with --show-trades.",
    )
    parser.add_argument(
        "--exchange-info",
        type=Path,
        default=None,
        help="Optional path to an exchangeInfo JSON file.",
    )
    return parser.parse_known_args(argv)


def has_option(args: list[str], *names: str) -> bool:
    for arg in args:
        for name in names:
            if arg == name or arg.startswith(f"{name}="):
                return True
    return False


def build_backtesting_argv(raw_args: list[str]) -> list[str]:
    args = list(raw_args)
    if args and args[0] == "backtesting":
        args = args[1:]

    if not has_option(args, "--config", "-c"):
        args = ["--config", str(DEFAULT_CONFIG), *args]
    if not has_option(args, "--user-data-dir"):
        args = ["--user-data-dir", str(DEFAULT_USER_DATA_DIR), *args]
    if not has_option(args, "--strategy-path"):
        args = ["--strategy-path", str(DEFAULT_STRATEGY_PATH), *args]

    return ["backtesting", *args]


def get_default_exchange_info_path(config: dict[str, Any]) -> Path:
    return (
        DEFAULT_FUTURES_EXCHANGE_INFO
        if str(config.get("trading_mode", "spot")) == "futures"
        else DEFAULT_SPOT_EXCHANGE_INFO
    )


def split_pair(pair: str) -> tuple[str, str, str | None]:
    if "/" not in pair:
        raise ValueError(f"Unsupported pair format: {pair!r}. Expected BASE/QUOTE.")
    base, quote_part = pair.split("/", 1)
    if ":" in quote_part:
        quote, settle = quote_part.split(":", 1)
        return base, quote, settle
    return base, quote_part, None


def pair_to_exchange_symbol(pair: str) -> str:
    base, quote, _settle = split_pair(pair)
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


def ensure_exchange_info_coverage(
    config: dict[str, Any], exchange_info_subset: dict[str, dict[str, Any]]
) -> None:
    if not exchange_info_subset:
        raise OperationalException(
            "mock_backtest now requires exchangeInfo metadata so markets can be parsed via ccxt. "
            "Provide --exchange-info PATH and make sure it contains the whitelisted pairs."
        )

    missing_pairs = [
        pair
        for pair in config.get("exchange", {}).get("pair_whitelist", [])
        if pair_to_exchange_symbol(pair) not in exchange_info_subset
    ]
    if missing_pairs:
        raise OperationalException(
            "exchangeInfo is available but missing metadata for these pairs: "
            f"{', '.join(missing_pairs)}. Update exchangeInfo or reduce the whitelist."
        )


def ensure_exchange_info_schema(
    config: dict[str, Any], exchange_info_subset: dict[str, dict[str, Any]]
) -> None:
    if str(config.get("trading_mode", "spot")) != "futures":
        return

    invalid_pairs = [
        pair
        for pair in config.get("exchange", {}).get("pair_whitelist", [])
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


def normalize_config_for_mock(config: dict[str, Any], raw_args: list[str]) -> dict[str, Any]:
    if not has_option(raw_args, "--strategy", "--strategy-list"):
        raise OperationalException("mock_backtest requires --strategy NAME or --strategy-list.")
    if not has_option(raw_args, "--timeframe"):
        raise OperationalException("mock_backtest requires --timeframe TIMEFRAME.")
    return config


def build_ccxt_market_parser(config: dict[str, Any]) -> ccxt.Exchange:
    exchange_name = str(config["exchange"]["name"])
    exchange_class = getattr(ccxt, exchange_name, None)
    if exchange_class is None:
        raise OperationalException(f"ccxt exchange '{exchange_name}' is not available.")

    exchange_config: dict[str, Any] = {
        "options": {
            "crossMarginPairsData": [],
            "isolatedMarginPairsData": [],
        }
    }
    if str(config.get("trading_mode", "spot")) == "futures":
        exchange_config["options"]["defaultType"] = get_default_exchange_options(config).get(
            "ccxt_futures_name", "swap"
        )
    return exchange_class(exchange_config)


def build_market_from_exchange_info(
    pair: str, config: dict[str, Any], symbol_info: dict[str, Any], parser: ccxt.Exchange
) -> dict[str, Any]:
    is_futures = str(config.get("trading_mode", "spot")) == "futures"
    market = dict(parser.parse_market(symbol_info))
    market["symbol"] = pair
    market["taker"] = config.get("fee", market.get("taker", 0.0002))
    market["maker"] = config.get("fee", market.get("maker", 0.0002))

    limits = dict(market.get("limits") or {})
    leverage_limits = dict(limits.get("leverage") or {})
    leverage_limits["min"] = leverage_limits.get("min") or 1
    leverage_limits["max"] = leverage_limits.get("max") or (100 if is_futures else 5)
    limits["leverage"] = leverage_limits

    if is_futures:
        market.setdefault("contractSize", 1)
    else:
        market["lot"] = market.get("precision", {}).get("amount")

    market["limits"] = limits
    return market


def build_markets(
    config: dict[str, Any], exchange_info_subset: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    markets: dict[str, dict[str, Any]] = {}
    market_parser = build_ccxt_market_parser(config)
    for pair in config.get("exchange", {}).get("pair_whitelist", []):
        symbol = pair_to_exchange_symbol(pair)
        symbol_info = exchange_info_subset.get(symbol)
        if not symbol_info:
            raise OperationalException(f"Pair {pair} is missing from exchangeInfo metadata.")
        markets[pair] = build_market_from_exchange_info(pair, config, symbol_info, market_parser)
    return markets


def get_default_exchange_options(config: dict[str, Any]) -> dict[str, Any]:
    exchange_name = str(config["exchange"]["name"])
    exchange_class_name = exchange_name.capitalize()
    include_futures = str(config.get("trading_mode", "spot")) == "futures"

    try:
        exchange_module = import_module(f"freqtrade.exchange.{exchange_name}")
        exchange_class = getattr(exchange_module, exchange_class_name)
    except (ImportError, AttributeError):
        exchange_class = Exchange

    return exchange_class.combine_ft_has(include_futures=include_futures)


def build_mock_ccxt_exchange(
    config: dict[str, Any], markets: dict[str, dict[str, Any]], timeframe_values: set[str]
) -> ccxt.Exchange:
    exchange_name = str(config["exchange"]["name"])
    exchange_class = getattr(ccxt, exchange_name, None)
    if exchange_class is None:
        raise OperationalException(f"ccxt exchange '{exchange_name}' is not available.")

    exchange_config: dict[str, Any] = {
        "options": {
            "crossMarginPairsData": [],
            "isolatedMarginPairsData": [],
        }
    }
    if str(config.get("trading_mode", "spot")) == "futures":
        exchange_config["options"]["defaultType"] = get_default_exchange_options(config).get(
            "ccxt_futures_name", "swap"
        )

    api = exchange_class(exchange_config)
    timeframes = {timeframe: timeframe for timeframe in sorted(timeframe_values)}
    api.timeframes = timeframes
    api.options.setdefault("timeframes", {})
    api.options["timeframes"].setdefault("spot", timeframes)
    api.options["timeframes"].setdefault("swap", timeframes)
    api.set_markets(markets)
    api.close = lambda: None
    return api


def mock_exchange_context(
    config: dict[str, Any], exchange_info_subset: dict[str, dict[str, Any]]
) -> ExitStack:
    timeframe_values = {"1m", "5m", "15m", "1h", "4h", "8h", "1d", str(config["timeframe"])}
    if config.get("timeframe_detail"):
        timeframe_values.add(str(config["timeframe_detail"]))
    markets = build_markets(config, exchange_info_subset)
    sync_api = build_mock_ccxt_exchange(config, markets, timeframe_values)
    async_api = build_mock_ccxt_exchange(config, markets, timeframe_values)
    ccxt_inits = iter([sync_api, async_api])
    stack = ExitStack()

    stack.enter_context(
        patch(
            f"{EXMS}._init_ccxt",
            side_effect=lambda exchange_conf, sync, ccxt_kwargs: next(ccxt_inits),
        )
    )
    stack.enter_context(patch(f"{EXMS}._load_async_markets", return_value=None))

    if str(config.get("trading_mode", "spot")) == "futures":
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
    script_args, freqtrade_args = parse_script_args(argv)

    parsed_args = Arguments(build_backtesting_argv(freqtrade_args)).get_parsed_arg()
    config = setup_optimize_configuration(parsed_args, RunMode.BACKTEST)
    config = normalize_config_for_mock(config, freqtrade_args)
    exchange_info_path = script_args.exchange_info or get_default_exchange_info_path(config)
    exchange_info_subset = load_exchange_info_subset(
        exchange_info_path,
        config.get("exchange", {}).get("pair_whitelist", []),
    )
    ensure_exchange_info_coverage(config, exchange_info_subset)
    ensure_exchange_info_schema(config, exchange_info_subset)

    with mock_exchange_context(config, exchange_info_subset):
        backtesting = Backtesting(config)
        try:
            backtesting.start()
            if script_args.show_trades:
                print_trade_tables(backtesting, script_args.trade_limit)
        finally:
            Backtesting.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())