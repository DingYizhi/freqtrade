"""
Run freqtrade backtesting against the real user_data configuration while mocking
exchange network calls.

Default paths injected by this script:
  --config D:/_code/ft_userdata/user_data/config.json
  --user-data-dir D:/_code/ft_userdata/user_data
  --strategy-path D:/_code/ft_userdata/user_data/strategies

Examples:
  python scripts/mock_backtest.py --strategy Mar3Strategy --timerange 20220101-20220108
  python scripts/mock_backtest.py --strategy Mar3Strategy --timerange 20220101-20220108 --pairs BTC/USDT:USDT
  python scripts/mock_backtest.py --strategy Mar3Strategy --timerange 20220101-20220108 --pairs BTC/USDT:USDT ETH/USDT:USDT --timeframe 1m --timeframe-detail 5m --enable-protections --cache none
  python scripts/mock_backtest.py --strategy Mar3Strategy --timerange 20220101-20220108 --export trades --backtest-directory D:/_code/ft_userdata/user_data/backtest_results --show-trades --trade-limit 5

Script-only options:
  --show-trades     Print the first N raw trades after the run.
  --trade-limit N   Maximum number of trades to print per strategy.
    --exchange-info   Optional Binance exchangeInfo.json path used to enrich markets.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

from freqtrade.commands.arguments import Arguments
from freqtrade.commands.optimize_commands import setup_optimize_configuration
from freqtrade.enums import CandleType, RunMode
from freqtrade.exceptions import OperationalException
from freqtrade.optimize.backtesting import Backtesting


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORK_ROOT = Path(r"D:\_code\ft_userdata")
DEFAULT_USER_DATA_DIR = DEFAULT_WORK_ROOT / "user_data"
DEFAULT_CONFIG = DEFAULT_USER_DATA_DIR / "config.json"
DEFAULT_STRATEGY_PATH = DEFAULT_USER_DATA_DIR / "strategies"
DEFAULT_EXCHANGE_INFO = DEFAULT_WORK_ROOT / "exchangeInfo.json"
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
        default=DEFAULT_EXCHANGE_INFO,
        help="Optional path to a Binance exchangeInfo.json file.",
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


def decimal_precision(value: str | None) -> int | float:
    if not value:
        return 8
    value = value.rstrip("0")
    if "." not in value:
        return 0
    return len(value.split(".", 1)[1])


def find_filter(symbol_info: dict[str, Any], filter_type: str) -> dict[str, Any]:
    for item in symbol_info.get("filters", []):
        if item.get("filterType") == filter_type:
            return item
    return {}


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


def infer_strategy_from_path(strategy_path: Path) -> str | None:
    strategy_files = sorted(
        path for path in strategy_path.glob("*.py") if path.name != "__init__.py"
    )
    if len(strategy_files) != 1:
        return None

    content = strategy_files[0].read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"class\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*IStrategy\s*\)", content)
    return match.group(1) if match else None


def find_strategy_file(strategy_path: Path, strategy_name: str) -> Path | None:
    pattern = re.compile(rf"class\s+{re.escape(strategy_name)}\s*\(\s*IStrategy\s*\)")
    for path in sorted(strategy_path.glob("*.py")):
        if path.name == "__init__.py":
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        if pattern.search(content):
            return path
    return None


def infer_timeframe_from_strategy(strategy_path: Path, strategy_name: str) -> str | None:
    strategy_file = find_strategy_file(strategy_path, strategy_name)
    if not strategy_file:
        return None

    content = strategy_file.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"^\s*timeframe\s*=\s*['\"]([^'\"]+)['\"]", content, re.MULTILINE)
    return match.group(1) if match else None


def infer_pairs_from_datadir(config: dict[str, Any]) -> list[str]:
    datadir = Path(config["datadir"])
    timeframe = str(config["timeframe"])
    trading_mode = str(config.get("trading_mode", "spot"))

    if trading_mode == "futures":
        search_dir = datadir / "futures" if (datadir / "futures").is_dir() else datadir
        pattern = f"*-{timeframe}-futures.*"
        pairs: list[str] = []
        for path in sorted(search_dir.glob(pattern)):
            stem = path.name
            suffix = f"-{timeframe}-futures"
            if "." in stem:
                stem = stem.split(".", 1)[0]
            if not stem.endswith(suffix):
                continue
            parts = stem[: -len(suffix)].split("_")
            if len(parts) >= 3:
                pairs.append(f"{parts[0]}/{parts[1]}:{parts[2]}")
        return list(dict.fromkeys(pairs))

    pattern = f"*-{timeframe}.*"
    pairs = []
    for path in sorted(datadir.glob(pattern)):
        stem = path.name
        suffix = f"-{timeframe}"
        if "." in stem:
            stem = stem.split(".", 1)[0]
        if stem.endswith("-trades") or not stem.endswith(suffix):
            continue
        parts = stem[: -len(suffix)].split("_")
        if len(parts) >= 2:
            pairs.append(f"{parts[0]}/{parts[1]}")
    return list(dict.fromkeys(pairs))


def normalize_config_for_mock(config: dict[str, Any]) -> dict[str, Any]:
    strategy_path = Path(config.get("strategy_path", DEFAULT_STRATEGY_PATH))
    if not config.get("strategy") and not config.get("strategy_list"):
        inferred_strategy = infer_strategy_from_path(strategy_path)
        if inferred_strategy:
            config["strategy"] = inferred_strategy
            print(f"Auto-selected strategy: {inferred_strategy}")
        else:
            raise OperationalException(
                "No strategy selected. Pass --strategy NAME or --strategy-list NAME1 NAME2."
            )

    if not config.get("timeframe") and config.get("strategy"):
        inferred_timeframe = infer_timeframe_from_strategy(strategy_path, config["strategy"])
        if inferred_timeframe:
            config["timeframe"] = inferred_timeframe
            print(f"Auto-selected timeframe from strategy: {inferred_timeframe}")
        else:
            raise OperationalException(
                "No timeframe found in config. Pass --timeframe or define timeframe in the strategy."
            )

    explicit_pairs = config.get("pairs") or config.get("exchange", {}).get("pair_whitelist") or []
    pair_whitelist = [pair for pair in explicit_pairs if pair]
    if not pair_whitelist:
        pair_whitelist = infer_pairs_from_datadir(config)

    if not pair_whitelist:
        raise OperationalException(
            f"No local pairs found in {config['datadir']} for timeframe {config['timeframe']}."
        )

    original_pairlists = config.get("pairlists", [])
    if original_pairlists != [{"method": "StaticPairList"}] or not config["exchange"].get(
        "pair_whitelist"
    ):
        print("Using StaticPairList for mocked backtesting.")

    config["exchange"]["pair_whitelist"] = pair_whitelist
    config["pairlists"] = [{"method": "StaticPairList"}]
    return config


def build_market_from_exchange_info(
    pair: str, config: dict[str, Any], symbol_info: dict[str, Any]
) -> dict[str, Any]:
    base, quote, settle = split_pair(pair)
    is_futures = str(config.get("trading_mode", "spot")) == "futures" or settle is not None
    price_filter = find_filter(symbol_info, "PRICE_FILTER")
    lot_filter = find_filter(symbol_info, "LOT_SIZE")
    notional_filter = find_filter(symbol_info, "NOTIONAL")

    precision_price = decimal_precision(price_filter.get("tickSize"))
    precision_amount = decimal_precision(lot_filter.get("stepSize"))
    min_amount = float(lot_filter.get("minQty", 0) or 0)
    max_amount = float(lot_filter.get("maxQty", 0) or 0) or None
    min_price = float(price_filter.get("minPrice", 0) or 0) or None
    max_price = float(price_filter.get("maxPrice", 0) or 0) or None
    min_cost = float(notional_filter.get("minNotional", 0) or 0) or None
    max_cost = float(notional_filter.get("maxNotional", 0) or 0) or None

    if is_futures:
        settle = settle or quote
        return {
            "id": symbol_info.get("symbol", f"{base}_{quote}"),
            "symbol": pair,
            "base": base,
            "quote": quote,
            "settle": settle,
            "baseId": symbol_info.get("baseAsset", base),
            "quoteId": symbol_info.get("quoteAsset", quote),
            "settleId": settle,
            "type": "swap",
            "spot": False,
            "margin": False,
            "swap": True,
            "future": True,
            "option": False,
            "contract": True,
            "linear": True,
            "inverse": False,
            "tierBased": False,
            "percentage": True,
            "taker": config.get("fee", 0.0002),
            "maker": config.get("fee", 0.0002),
            "contractSize": 1,
            "active": symbol_info.get("status") == "TRADING",
            "expiry": None,
            "expiryDatetime": None,
            "strike": None,
            "optionType": None,
            "limits": {
                "leverage": {"min": 1, "max": 100},
                "amount": {"min": max(min_amount, 1), "max": max_amount},
                "price": {"min": min_price, "max": max_price},
                "cost": {"min": min_cost, "max": max_cost},
            },
            "precision": {"price": precision_price, "amount": precision_amount},
            "info": {},
        }

    return {
        "id": symbol_info.get("symbol", f"{base}{quote}".lower()),
        "symbol": pair,
        "base": base,
        "quote": quote,
        "active": symbol_info.get("status") == "TRADING",
        "spot": symbol_info.get("isSpotTradingAllowed", True),
        "margin": symbol_info.get("isMarginTradingAllowed", False),
        "swap": False,
        "future": False,
        "option": False,
        "contract": None,
        "linear": None,
        "inverse": None,
        "taker": config.get("fee", 0.0002),
        "maker": config.get("fee", 0.0002),
        "contractSize": None,
        "precision": {
            "price": precision_price,
            "amount": precision_amount,
            "cost": precision_price,
        },
        "lot": float(lot_filter.get("stepSize", 0.00000001) or 0.00000001),
        "limits": {
            "leverage": {"min": 1, "max": 5},
            "amount": {"min": min_amount, "max": max_amount},
            "price": {"min": min_price, "max": max_price},
            "cost": {"min": min_cost, "max": max_cost},
        },
        "info": {},
    }


def build_market(pair: str, config: dict[str, Any]) -> dict[str, Any]:
    base, quote, settle = split_pair(pair)
    is_futures = str(config.get("trading_mode", "spot")) == "futures" or settle is not None

    if is_futures:
        settle = settle or quote
        return {
            "id": f"{base}_{quote}",
            "symbol": pair,
            "base": base,
            "quote": quote,
            "settle": settle,
            "baseId": base,
            "quoteId": quote,
            "settleId": settle,
            "type": "swap",
            "spot": False,
            "margin": False,
            "swap": True,
            "future": True,
            "option": False,
            "contract": True,
            "linear": True,
              "inverse": False,
            "tierBased": False,
            "percentage": True,
            "taker": config.get("fee", 0.0002),
            "maker": config.get("fee", 0.0002),
            "contractSize": 1,
            "active": True,
            "expiry": None,
            "expiryDatetime": None,
            "strike": None,
            "optionType": None,
            "limits": {
                "leverage": {"min": 1, "max": 100},
                "amount": {"min": 1, "max": 1000000},
                "price": {"min": None, "max": None},
                "cost": {"min": None, "max": None},
            },
            "precision": {"price": 0.01, "amount": 1},
            "info": {},
        }

    return {
        "id": f"{base}{quote}".lower(),
        "symbol": pair,
        "base": base,
        "quote": quote,
        "active": True,
        "spot": True,
        "margin": True,
        "swap": False,
        "future": False,
        "option": False,
        "contract": None,
        "linear": None,
        "inverse": None,
        "taker": config.get("fee", 0.0002),
        "maker": config.get("fee", 0.0002),
        "contractSize": None,
        "precision": {"price": 8, "amount": 8, "cost": 8},
        "lot": 0.00000001,
        "limits": {
            "leverage": {"min": 1, "max": 5},
            "amount": {"min": 0.01, "max": 100000000},
            "price": {"min": None, "max": 500000},
            "cost": {"min": 0.0001, "max": 500000},
        },
        "info": {},
    }


def build_markets(
    config: dict[str, Any], exchange_info_subset: dict[str, dict[str, Any]] | None = None
) -> dict[str, dict[str, Any]]:
    exchange_info_subset = exchange_info_subset or {}
    markets: dict[str, dict[str, Any]] = {}
    for pair in config.get("exchange", {}).get("pair_whitelist", []):
        symbol = pair_to_exchange_symbol(pair)
        symbol_info = exchange_info_subset.get(symbol)
        markets[pair] = (
            build_market_from_exchange_info(pair, config, symbol_info)
            if symbol_info
            else build_market(pair, config)
        )
    return markets


def build_tickers(config: dict[str, Any]) -> dict[str, dict[str, float]]:
    tickers = {}
    for pair in config.get("exchange", {}).get("pair_whitelist", []):
        tickers[pair] = {
            "bid": 1.0,
            "ask": 1.0,
            "last": 1.0,
            "quoteVolume": 1_000_000.0,
        }
    return tickers


def build_exchange_options(config: dict[str, Any]) -> dict[str, Any]:
    options = {
        "uses_leverage_tiers": True,
    }
    if str(config.get("trading_mode", "spot")) == "futures":
        options.update(
            {
                "funding_fee_timeframe": "8h",
                "mark_ohlcv_timeframe": "8h",
                "mark_ohlcv_price": "mark",
            }
        )
    return options


def mock_exchange_context(
    config: dict[str, Any], exchange_info_subset: dict[str, dict[str, Any]] | None = None
) -> ExitStack:
    exchange_name = str(config["exchange"]["name"])
    exchange_class = exchange_name.capitalize()
    timeframe_values = {"1m", "5m", "15m", "1h", "4h", "8h", "1d", str(config["timeframe"])}
    if config.get("timeframe_detail"):
        timeframe_values.add(str(config["timeframe_detail"]))
    markets = build_markets(config, exchange_info_subset)
    tickers = build_tickers(config)
    exchange_options = build_exchange_options(config)
    stack = ExitStack()

    stack.enter_context(patch(f"{EXMS}.validate_config", MagicMock()))
    stack.enter_context(patch(f"{EXMS}.validate_timeframes", MagicMock()))
    stack.enter_context(patch(f"{EXMS}.id", new_callable=PropertyMock, return_value=exchange_name))
    stack.enter_context(
        patch(f"{EXMS}.name", new_callable=PropertyMock, return_value=exchange_name.title())
    )
    stack.enter_context(patch(f"{EXMS}.precisionMode", new_callable=PropertyMock, return_value=2))
    stack.enter_context(
        patch(f"{EXMS}.precision_mode_price", new_callable=PropertyMock, return_value=2)
    )
    stack.enter_context(patch("freqtrade.exchange.bybit.Bybit.cache_leverage_tiers"))
    stack.enter_context(patch(f"{EXMS}._load_async_markets", return_value=markets))
    stack.enter_context(patch(f"{EXMS}.markets", new_callable=PropertyMock, return_value=markets))
    stack.enter_context(
        patch(
            f"freqtrade.exchange.{exchange_name}.{exchange_class}._supported_trading_mode_margin_pairs",
            new_callable=PropertyMock,
            return_value=[
                ("spot", ""),
                ("margin", "cross"),
                ("margin", "isolated"),
                ("futures", "cross"),
                ("futures", "isolated"),
            ],
        )
    )
    stack.enter_context(patch(f"{EXMS}.get_fee", return_value=config.get("fee", 0.0002)))
    stack.enter_context(patch(f"{EXMS}.get_tickers", return_value=tickers))
    stack.enter_context(patch(f"{EXMS}._init_ccxt", MagicMock()))
    stack.enter_context(
        patch(f"{EXMS}.timeframes", new_callable=PropertyMock, return_value=sorted(timeframe_values))
    )
    stack.enter_context(patch(f"{EXMS}.get_min_pair_stake_amount", return_value=0.00001))
    stack.enter_context(patch(f"{EXMS}.get_max_pair_stake_amount", return_value=float("inf")))
    stack.enter_context(
        patch(f"{EXMS}.get_pair_base_currency", side_effect=lambda pair: split_pair(pair)[0])
    )
    stack.enter_context(
        patch(
            f"{EXMS}.get_option",
            side_effect=lambda key, default=None: exchange_options.get(key, default),
        )
    )

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
    config = normalize_config_for_mock(config)
    exchange_info_subset = load_exchange_info_subset(
        script_args.exchange_info,
        config.get("exchange", {}).get("pair_whitelist", []),
    )

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