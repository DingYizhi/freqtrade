# pragma pylint: disable=W0603
"""
Cryptocurrency Exchanges support
"""

import logging
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from math import isnan
from typing import Any, Literal, TypeVar

import ccxt
from ccxt import TICK_SIZE
from dateutil import parser
from pandas import DataFrame

from freqtrade.configuration import remove_exchange_credentials
from freqtrade.constants import (
    DEFAULT_AMOUNT_RESERVE_PERCENT,
    Config,
    ExchangeConfig,
    MakerTaker,
    PairWithTimeframe,
)
from freqtrade.enums import (
    OPTIMIZE_MODES,
    CandleType,
    MarginMode,
    RunMode,
    TradingMode,
)
from freqtrade.exceptions import (
    ConfigurationError,
    DDosProtection,
    ExchangeError,
    InvalidOrderException,
    OperationalException,
    TemporaryError,
)
from freqtrade.exchange.common import (
    retrier,
)
from freqtrade.exchange.exchange_types import (
    FtHas,
    LeverageTier,
)
from freqtrade.exchange.exchange_utils import (
    ROUND,
    ROUND_DOWN,
    ROUND_UP,
    amount_to_contract_precision,
    amount_to_contracts,
    amount_to_precision,
    contracts_to_amount,
    date_minus_candles,
    is_exchange_known_ccxt,
    market_is_active,
    price_to_precision,
)
from freqtrade.exchange.exchange_utils_timeframe import (
    timeframe_to_minutes,
)
from freqtrade.misc import (
    deep_merge_dicts,
    file_dump_json,
    file_load_json,
    safe_value_nested,
)
from freqtrade.util.datetime_helpers import dt_ts


logger = logging.getLogger(__name__)

T = TypeVar("T")


class Exchange:
    # Parameters to add directly to buy/sell calls (like agreeing to trading agreement)
    _params: dict = {}

    # Additional parameters - added to the ccxt object
    _ccxt_params: dict = {}

    # Dict to specify which options each exchange implements
    # This defines defaults, which can be selectively overridden by subclasses using _ft_has
    # or by specifying them in the configuration.
    _ft_has_default: FtHas = {
        "stoploss_on_exchange": False,
        "stop_price_param": "stopLossPrice",  # Used for stoploss_on_exchange request
        "stop_price_prop": "stopLossPrice",  # Used for stoploss_on_exchange response parsing
        "stoploss_order_types": {},
        "stoploss_blocks_assets": True,  # By default stoploss orders block assets
        "stoploss_query_requires_stop_flag": False,  # Require "stop": True" to fetch stop orders
        "order_time_in_force": ["GTC"],
        "ohlcv_params": {},
        "ohlcv_has_history": True,  # Some exchanges (Kraken) don't provide history via ohlcv
        "ohlcv_partial_candle": True,
        "ohlcv_require_since": False,
        "always_require_api_keys": False,  # purge API keys for Dry-run. Must default to false.
        # Check https://github.com/ccxt/ccxt/issues/10767 for removal of ohlcv_volume_currency
        "ohlcv_volume_currency": "base",  # "base" or "quote"
        "tickers_have_quoteVolume": True,
        "tickers_have_percentage": True,
        "tickers_have_bid_ask": True,  # bid / ask empty for fetch_tickers
        "tickers_have_price": True,
        "trades_limit": 1000,  # Limit for 1 call to fetch_trades
        "trades_pagination": "time",  # Possible are "time" or "id"
        "trades_pagination_arg": "since",
        "trades_has_history": False,
        "l2_limit_range": None,
        "l2_limit_range_required": True,  # Allow Empty L2 limit (kucoin)
        "l2_limit_upper": None,  # Upper limit for L2 limit
        "mark_ohlcv_price": "mark",
        "mark_ohlcv_timeframe": "1h",
        "funding_fee_timeframe": "1h",
        "ccxt_futures_name": "swap",
        "needs_trading_fees": False,  # use fetch_trading_fees to cache fees
        "order_props_in_contracts": ["amount", "filled", "remaining"],
        "fetch_orders_limit_minutes": None,  # "fetch_orders" is not time-limited by default
        # Override createMarketBuyOrderRequiresPrice where ccxt has it wrong
        "marketOrderRequiresPrice": False,
        "exchange_has_overrides": {},  # Dictionary overriding ccxt's "has".
        "proxy_coin_mapping": {},  # Mapping for proxy coins
        "has_delisting": False,  # Set to true for exchanges that have delisting pair checks
    }
    _ft_has: FtHas = {}
    _ft_has_futures: FtHas = {}

    _supported_trading_mode_margin_pairs: list[tuple[TradingMode, MarginMode]] = [
        # Non-defined exchanges only support spot mode.
        (TradingMode.SPOT, MarginMode.NONE),
    ]

    def __init__(
        self,
        config: Config,
        *,
        exchange_config: ExchangeConfig | None = None,
        validate: bool = True,
        load_leverage_tiers: bool = False,
    ) -> None:
        """
        Initializes this module with the given config,
        it does basic validation whether the specified exchange and pairs are valid.
        :return: None
        """
        self._api: ccxt.Exchange
        self._markets: dict = {}
        self._leverage_tiers: dict[str, list[LeverageTier]] = {}
        self._config: Config = config

        # Leverage properties
        self.trading_mode: TradingMode = TradingMode(
            self._config.get("trading_mode", self._supported_trading_mode_margin_pairs[0][0])
        )
        self.margin_mode: MarginMode = MarginMode(
            self._config["margin_mode"]
            if self._config.get("margin_mode")
            else self._supported_trading_mode_margin_pairs[0][1]
        )
        self._config["trading_mode"] = self.trading_mode
        self._config["margin_mode"] = self.margin_mode
        self._config["candle_type_def"] = CandleType.get_default(self.trading_mode)
        self.liquidation_buffer = self._config.get("liquidation_buffer", 0.05)

        exchange_conf: ExchangeConfig = (
            exchange_config if exchange_config else self._config["exchange"]
        )

        # Deep merge ft_has with default ft_has options
        # Must be called before ft_has is used.
        self.build_ft_has(exchange_conf)

        # Timestamp of last markets refresh
        self._last_markets_refresh: int = 0

        # Holds candles
        self._klines: dict[PairWithTimeframe, DataFrame] = {}

        logger.info(f"Using CCXT {ccxt.__version__}")

        # Don't remove exchange credentials for dry-run or if always_require_api_keys is set
        remove_exchange_credentials(
            exchange_conf,
            not self._ft_has["always_require_api_keys"] and self._config.get("dry_run", False),
        )
        # Initialize ccxt objects
        ccxt_config = self._ccxt_config
        ccxt_config = deep_merge_dicts(exchange_conf.get("ccxt_config", {}), ccxt_config)
        ccxt_config = deep_merge_dicts(exchange_conf.get("ccxt_sync_config", {}), ccxt_config)

        self._api = self._init_ccxt(exchange_conf, True, ccxt_config)

        logger.info(f'Using Exchange "{self.name}"')
        self.required_candle_call_count = 1
        # Converts the interval provided in minutes in config to seconds
        self.markets_refresh_interval: int = (
            exchange_conf.get("markets_refresh_interval", 60) * 60 * 1000
        )

        if validate:
            # Initial markets load
            self.reload_markets(True, load_leverage_tiers=False)
            self.validate_config(self._config)

        if self.trading_mode != TradingMode.SPOT and load_leverage_tiers:
            self.fill_leverage_tiers()
        self.ft_additional_exchange_init()

    def _set_startup_candle_count(self, config: Config) -> None:
        self._startup_candle_count: int = config.get("startup_candle_count", 0)
        self.required_candle_call_count = self.validate_required_startup_candles(
            self._startup_candle_count, config.get("timeframe", "")
        )

    def validate_config(self, config: Config) -> None:
        # Check if timeframe is available
        self.validate_timeframes(config.get("timeframe"))

        # Check if all pairs are available
        self.validate_stakecurrency(config["stake_currency"])
        self.validate_trading_mode_and_margin_mode(self.trading_mode, self.margin_mode)

        self._set_startup_candle_count(config)

    def _init_ccxt(
        self, exchange_config: dict[str, Any], sync: bool, ccxt_kwargs: dict[str, Any]
    ) -> ccxt.Exchange:
        """
        Initialize ccxt with given config and return valid ccxt instance.
        """
        # Find matching class for the given exchange name
        name = exchange_config["name"]
        ccxt_module = ccxt

        if not is_exchange_known_ccxt(name, ccxt_module):
            raise OperationalException(f"Exchange {name} is not supported by ccxt")

        ex_config = {
            "apiKey": exchange_config.get(
                "api_key", exchange_config.get("apiKey", exchange_config.get("key"))
            ),
            "secret": exchange_config.get("secret"),
            "password": exchange_config.get("password"),
            "uid": exchange_config.get("uid", ""),
            "accountId": exchange_config.get("account_id", exchange_config.get("accountId", "")),
            # DEX attributes:
            "walletAddress": exchange_config.get(
                "wallet_address", exchange_config.get("walletAddress")
            ),
            "privateKey": exchange_config.get("private_key", exchange_config.get("privateKey")),
        }
        if ccxt_kwargs:
            logger.info("Applying additional ccxt config: %s", ccxt_kwargs)
        if self._ccxt_params:
            # Inject static options after the above output to not confuse users.
            ccxt_kwargs = deep_merge_dicts(self._ccxt_params, deepcopy(ccxt_kwargs))
        if ccxt_kwargs:
            ex_config.update(ccxt_kwargs)
        try:
            api = getattr(ccxt_module, name.lower())(ex_config)
        except (KeyError, AttributeError) as e:
            raise OperationalException(f"Exchange {name} is not supported") from e
        except ccxt.BaseError as e:
            raise OperationalException(f"Initialization of ccxt failed. Reason: {e}") from e

        return api

    @property
    def _ccxt_config(self) -> dict:
        # Parameters to add directly to ccxt sync/async initialization.
        if self.trading_mode == TradingMode.MARGIN:
            return {"options": {"defaultType": "margin"}}
        elif self.trading_mode == TradingMode.FUTURES:
            return {"options": {"defaultType": self._ft_has["ccxt_futures_name"]}}
        else:
            return {}

    @property
    def name(self) -> str:
        """exchange Name (from ccxt)"""
        return self._api.name

    @property
    def id(self) -> str:
        """exchange ccxt id"""
        return self._api.id

    @property
    def timeframes(self) -> list[str]:
        market_type = (
            "spot"
            if self.trading_mode != TradingMode.FUTURES
            else self._ft_has["ccxt_futures_name"]
        )
        timeframes = self._api.options.get("timeframes", {}).get(market_type)
        if timeframes is None:
            timeframes = self._api.timeframes
        return list((timeframes or {}).keys())

    @property
    def markets(self) -> dict[str, Any]:
        """exchange ccxt markets"""
        if not self._markets:
            logger.info("Markets were not loaded. Loading them now..")
            self.reload_markets(True)
        return self._markets

    @property
    def precisionMode(self) -> int:
        """Exchange ccxt precisionMode"""
        return self._api.precisionMode

    @property
    def precision_mode_price(self) -> int:
        """
        Exchange ccxt precisionMode used for price
        Workaround for ccxt limitation to not have precisionMode for price
        if it differs for an exchange
        Might need to be updated if https://github.com/ccxt/ccxt/issues/20408 is fixed.
        """
        return self._api.precisionMode

    def ft_additional_exchange_init(self) -> None:
        """
        Wrapper around additional_exchange_init to simplify testing
        """
        self.additional_exchange_init()

    def additional_exchange_init(self) -> None:
        """
        Additional exchange initialization logic.
        .api will be available at this point.
        Must be overridden in child methods if required.
        """
        pass

    def ohlcv_candle_limit(
        self, timeframe: str, candle_type: CandleType, since_ms: int | None = None
    ) -> int:
        """
        Exchange ohlcv candle limit
        Uses ohlcv_candle_limit_per_timeframe if the exchange has different limits
        per timeframe (e.g. bittrex), otherwise falls back to ohlcv_candle_limit
        :param timeframe: Timeframe to check
        :param candle_type: Candle-type
        :param since_ms: Starting timestamp
        :return: Candle limit as integer
        """

        ccxt_val = self.features(
            "spot" if candle_type == CandleType.SPOT else "futures", "fetchOHLCV", "limit", 500
        )
        if not isinstance(ccxt_val, float | int):
            ccxt_val = 500
        fallback_val = self._ft_has.get("ohlcv_candle_limit", ccxt_val)
        if candle_type == CandleType.FUNDING_RATE:
            fallback_val = self._ft_has.get("funding_fee_candle_limit", fallback_val)
        return int(
            self._ft_has.get("ohlcv_candle_limit_per_timeframe", {}).get(
                timeframe, str(fallback_val)
            )
        )

    def get_markets(
        self,
        base_currencies: list[str] | None = None,
        quote_currencies: list[str] | None = None,
        spot_only: bool = False,
        margin_only: bool = False,
        futures_only: bool = False,
        tradable_only: bool = True,
        active_only: bool = False,
    ) -> dict[str, Any]:
        """
        Return exchange ccxt markets, filtered out by base currency and quote currency
        if this was requested in parameters.
        """
        markets = self.markets
        if not markets:
            raise OperationalException("Markets were not loaded.")

        if base_currencies:
            markets = {k: v for k, v in markets.items() if v["base"] in base_currencies}
        if quote_currencies:
            markets = {k: v for k, v in markets.items() if v["quote"] in quote_currencies}
        if tradable_only:
            markets = {k: v for k, v in markets.items() if self.market_is_tradable(v)}
        if spot_only:
            markets = {k: v for k, v in markets.items() if self.market_is_spot(v)}
        if margin_only:
            markets = {k: v for k, v in markets.items() if self.market_is_margin(v)}
        if futures_only:
            markets = {k: v for k, v in markets.items() if self.market_is_future(v)}
        if active_only:
            markets = {k: v for k, v in markets.items() if market_is_active(v)}
        return markets

    def get_quote_currencies(self) -> list[str]:
        """
        Return a list of supported quote currencies
        """
        markets = self.markets
        return sorted(set([x["quote"] for _, x in markets.items()]))

    def get_pair_quote_currency(self, pair: str) -> str:
        """Return a pair's quote currency (base/quote:settlement)"""
        return self.markets.get(pair, {}).get("quote", "")

    def get_pair_base_currency(self, pair: str) -> str:
        """Return a pair's base currency (base/quote:settlement)"""
        return self.markets.get(pair, {}).get("base", "")

    def market_is_future(self, market: dict[str, Any]) -> bool:
        return (
            market.get(self._ft_has["ccxt_futures_name"], False) is True
            and market.get("type", False) == "swap"
            and market.get("linear", False) is True
        )

    def market_is_spot(self, market: dict[str, Any]) -> bool:
        return market.get("spot", False) is True

    def market_is_margin(self, market: dict[str, Any]) -> bool:
        return market.get("margin", False) is True

    def market_is_tradable(self, market: dict[str, Any]) -> bool:
        """
        Check if the market symbol is tradable by Freqtrade.
        Ensures that Configured mode aligns to
        """
        return (
            market.get("quote", None) is not None
            and market.get("base", None) is not None
            and (
                self.precisionMode != TICK_SIZE
                # Too low precision will falsify calculations
                or market.get("precision", {}).get("price") is None
                or market.get("precision", {}).get("price") > 1e-11
            )
            and (
                (self.trading_mode == TradingMode.SPOT and self.market_is_spot(market))
                or (self.trading_mode == TradingMode.MARGIN and self.market_is_margin(market))
                or (self.trading_mode == TradingMode.FUTURES and self.market_is_future(market))
            )
        )

    def klines(self, pair_interval: PairWithTimeframe, copy: bool = True) -> DataFrame:
        if pair_interval in self._klines:
            return self._klines[pair_interval].copy() if copy else self._klines[pair_interval]
        else:
            return DataFrame()

    def get_contract_size(self, pair: str) -> float | None:
        if self.trading_mode == TradingMode.FUTURES:
            market = self.markets.get(pair, {})
            contract_size: float = 1.0
            if not market:
                return None
            if market.get("contractSize") is not None:
                # ccxt has contractSize in markets as string
                contract_size = float(market["contractSize"])
            return contract_size
        else:
            return 1

    def _amount_to_contracts(self, pair: str, amount: float) -> float:
        contract_size = self.get_contract_size(pair)
        return amount_to_contracts(amount, contract_size)

    def _contracts_to_amount(self, pair: str, num_contracts: float) -> float:
        contract_size = self.get_contract_size(pair)
        return contracts_to_amount(num_contracts, contract_size)

    def amount_to_contract_precision(self, pair: str, amount: float) -> float:
        """
        Helper wrapper around amount_to_contract_precision
        """
        contract_size = self.get_contract_size(pair)

        return amount_to_contract_precision(
            amount, self.get_precision_amount(pair), self.precisionMode, contract_size
        )

    def reload_markets(self, force: bool = False, *, load_leverage_tiers: bool = True) -> None:
        """
        Reload / Initialize markets if refresh interval has passed (sync only for backtest).
        """
        is_initial = self._last_markets_refresh == 0
        if (
            not force
            and self._last_markets_refresh > 0
            and (self._last_markets_refresh + self.markets_refresh_interval > dt_ts())
        ):
            return None
        logger.debug("Performing scheduled market reload..")
        try:
            self._api.load_markets(reload=True, params={})
            self._markets = self._api.markets
            self._last_markets_refresh = dt_ts()

            if load_leverage_tiers and self.trading_mode == TradingMode.FUTURES:
                self.fill_leverage_tiers()
        except ccxt.BaseError:
            logger.exception("Could not load markets.")

    def validate_stakecurrency(self, stake_currency: str) -> None:
        """
        Checks stake-currency against available currencies on the exchange.
        Only runs on startup. If markets have not been loaded, there's been a problem with
        the connection to the exchange.
        :param stake_currency: Stake-currency to validate
        :raise: OperationalException if stake-currency is not available.
        """
        if not self._markets:
            raise OperationalException(
                "Could not load markets, therefore cannot start. "
                "Please investigate the above error for more details."
            )
        quote_currencies = self.get_quote_currencies()
        if stake_currency not in quote_currencies:
            raise ConfigurationError(
                f"{stake_currency} is not available as stake on {self.name}. "
                f"Available currencies are: {', '.join(quote_currencies)}"
            )

    def validate_timeframes(self, timeframe: str | None) -> None:
        """
        Check if timeframe from config is a supported timeframe on the exchange
        """
        if not hasattr(self._api, "timeframes") or self._api.timeframes is None:
            # If timeframes attribute is missing (or is None), the exchange probably
            # has no fetchOHLCV method.
            # Therefore we also show that.
            raise OperationalException(
                f"The ccxt library does not provide the list of timeframes "
                f"for the exchange {self.name} and this exchange "
                f"is therefore not supported. ccxt fetchOHLCV: {self.exchange_has('fetchOHLCV')}"
            )

        if timeframe and (timeframe not in self.timeframes):
            raise ConfigurationError(
                f"Invalid timeframe '{timeframe}'. This exchange supports: {self.timeframes}"
            )

        if (
            timeframe
            and self._config["runmode"] != RunMode.UTIL_EXCHANGE
            and timeframe_to_minutes(timeframe) < 1
        ):
            raise ConfigurationError("Timeframes < 1m are currently not supported by Freqtrade.")

    def validate_required_startup_candles(self, startup_candles: int, timeframe: str) -> int:
        """
        Checks if required startup_candles is more than ohlcv_candle_limit().
        Requires a grace-period of 5 candles - so a startup-period up to 494 is allowed by default.
        """

        candle_limit = self.ohlcv_candle_limit(
            timeframe,
            self._config["candle_type_def"],
            dt_ts(date_minus_candles(timeframe, startup_candles)) if timeframe else None,
        )
        # Require one more candle - to account for the still open candle.
        candle_count = startup_candles + 1
        # Allow 5 calls to the exchange per pair
        required_candle_call_count = int(
            (candle_count / candle_limit) + (0 if candle_count % candle_limit == 0 else 1)
        )
        if self._ft_has["ohlcv_has_history"]:
            if required_candle_call_count > 5:
                # Only allow 5 calls per pair to somewhat limit the impact
                raise ConfigurationError(
                    f"This strategy requires {startup_candles} candles to start, "
                    f"which is more than 5x ({candle_limit * 5 - 1} candles) "
                    f"the amount of candles {self.name} provides for {timeframe}."
                )
        elif required_candle_call_count > 1:
            raise ConfigurationError(
                f"This strategy requires {startup_candles} candles to start, "
                f"which is more than ({candle_limit - 1} candles) "
                f"the amount of candles {self.name} provides for {timeframe}."
            )
        if required_candle_call_count > 1:
            logger.warning(
                f"Using {required_candle_call_count} calls to get OHLCV. "
                f"This can result in slower operations for the bot. Please check "
                f"if you really need {startup_candles} candles for your strategy."
            )
        return required_candle_call_count

    def validate_trading_mode_and_margin_mode(
        self,
        trading_mode: TradingMode,
        margin_mode: MarginMode | None,  # Only None when trading_mode = TradingMode.SPOT
        allow_none_margin_mode: bool = False,
    ):
        """
        Checks if freqtrade can perform trades using the configured
        trading mode(Margin, Futures) and MarginMode(Cross, Isolated)
        Throws OperationalException:
            If the trading_mode/margin_mode type are not supported by freqtrade on this exchange
        """
        if trading_mode == TradingMode.SPOT:
            return
        if allow_none_margin_mode and margin_mode is None:
            # Verify trading mode independent of margin mode
            if not any(
                trading_mode == pair[0] for pair in self._supported_trading_mode_margin_pairs
            ):
                raise ConfigurationError(
                    f"Freqtrade does not support '{trading_mode}' on {self.name}."
                )

        if not allow_none_margin_mode and (
            (trading_mode, margin_mode) not in self._supported_trading_mode_margin_pairs
        ):
            mm_value = margin_mode and margin_mode.value
            raise ConfigurationError(
                f"Freqtrade does not support '{mm_value}' '{trading_mode}' on {self.name}."
            )

    @classmethod
    def combine_ft_has(cls, include_futures: bool) -> FtHas:
        """
        Combine all ft_has options from the class hierarchy.
        Child classes override parent classes.
        Doesn't apply overrides from the configuration.
        """
        _ft_has = deep_merge_dicts(cls._ft_has, deepcopy(cls._ft_has_default))

        if include_futures:
            _ft_has = deep_merge_dicts(cls._ft_has_futures, _ft_has)
        return _ft_has

    def build_ft_has(self, exchange_conf: ExchangeConfig) -> None:
        """
        Deep merge ft_has with default ft_has options
        and with exchange_conf._ft_has_params if available.
        This is called on initialization of the exchange object.
        It must be called before ft_has is used.
        """
        self._ft_has = self.combine_ft_has(include_futures=self.trading_mode == TradingMode.FUTURES)

        if exchange_conf.get("_ft_has_params"):
            self._ft_has = deep_merge_dicts(exchange_conf.get("_ft_has_params"), self._ft_has)
            logger.info("Overriding exchange._ft_has with config params, result: %s", self._ft_has)

    def get_option(self, param: str, default: Any | None = None) -> Any:
        """
        Get parameter value from _ft_has
        """
        return self._ft_has.get(param, default)

    def exchange_has(self, endpoint: str) -> bool:
        """
        Checks if exchange implements a specific API endpoint.
        Wrapper around ccxt 'has' attribute
        :param endpoint: Name of endpoint (e.g. 'fetchOHLCV', 'fetchTickers')
        :return: bool
        """
        if endpoint in self._ft_has.get("exchange_has_overrides", {}):
            return self._ft_has["exchange_has_overrides"][endpoint]
        return endpoint in self._api.has and self._api.has[endpoint]

    def features(
        self, market_type: Literal["spot", "futures"], endpoint, attribute, default: T
    ) -> T:
        """
        Returns the exchange features for the given markettype
        https://docs.ccxt.com/#/README?id=features
        attributes are in a nested dict, with spot and swap.linear
        e.g. spot.fetchOHLCV.limit
             swap.linear.fetchOHLCV.limit
        """
        feat = (
            safe_value_nested(self._api.features, "spot", {})
            if market_type == "spot"
            else safe_value_nested(self._api.features, "swap.linear", {})
        )

        return safe_value_nested(feat, f"{endpoint}.{attribute}", default)

    def get_precision_amount(self, pair: str) -> float | None:
        """
        Returns the amount precision of the exchange.
        :param pair: Pair to get precision for
        :return: precision for amount or None. Must be used in combination with precisionMode
        """
        return self.markets.get(pair, {}).get("precision", {}).get("amount", None)

    def get_precision_price(self, pair: str) -> float | None:
        """
        Returns the price precision of the exchange.
        :param pair: Pair to get precision for
        :return: precision for price or None. Must be used in combination with precisionMode
        """
        return self.markets.get(pair, {}).get("precision", {}).get("price", None)

    def amount_to_precision(self, pair: str, amount: float) -> float:
        """
        Returns the amount to buy or sell to a precision the Exchange accepts

        """
        return amount_to_precision(amount, self.get_precision_amount(pair), self.precisionMode)

    def price_to_precision(self, pair: str, price: float, *, rounding_mode: int = ROUND) -> float:
        """
        Returns the price rounded to the precision the Exchange accepts.
        The default price_rounding_mode in conf is ROUND.
        For stoploss calculations, must use ROUND_UP for longs, and ROUND_DOWN for shorts.
        """
        return price_to_precision(
            price,
            self.get_precision_price(pair),
            self.precision_mode_price,
            rounding_mode=rounding_mode,
        )

    def price_get_one_pip(self, pair: str, price: float) -> float:
        """
        Gets the "1 pip" value for this pair.
        Used in PriceFilter to calculate the 1pip movements.
        """
        precision = self.markets[pair]["precision"]["price"]
        if self.precisionMode == TICK_SIZE:
            return precision
        else:
            return 1 / pow(10, precision)

    def get_min_pair_stake_amount(
        self, pair: str, price: float, stoploss: float, leverage: float = 1.0
    ) -> float | None:
        return self._get_stake_amount_limit(pair, price, stoploss, "min", leverage)

    def get_max_pair_stake_amount(self, pair: str, price: float, leverage: float = 1.0) -> float:
        max_stake_amount = self._get_stake_amount_limit(pair, price, 0.0, "max", leverage)
        if max_stake_amount is None:
            # * Should never be executed
            raise OperationalException(
                f"{self.name}.get_max_pair_stake_amount should never set max_stake_amount to None"
            )
        return max_stake_amount

    def _get_stake_amount_limit(
        self,
        pair: str,
        price: float,
        stoploss: float,
        limit: Literal["min", "max"],
        leverage: float = 1.0,
    ) -> float | None:
        isMin = limit == "min"

        try:
            market = self.markets[pair]
        except KeyError:
            raise ValueError(f"Can't get market information for symbol {pair}")

        stake_limits = []
        limits = market["limits"]
        if isMin:
            # reserve some percent defined in config (5% default) + stoploss
            margin_reserve: float = 1.0 + self._config.get(
                "amount_reserve_percent", DEFAULT_AMOUNT_RESERVE_PERCENT
            )
            stoploss_reserve = margin_reserve / (1 - abs(stoploss)) if abs(stoploss) != 1 else 1.5
            # it should not be more than 50%
            stoploss_reserve = max(min(stoploss_reserve, 1.5), 1)
        else:
            # is_max
            margin_reserve = 1.0
            stoploss_reserve = 1.0
            if max_from_tiers := self._get_max_notional_from_tiers(pair, leverage=leverage):
                stake_limits.append(max_from_tiers)

        if limits["cost"][limit] is not None:
            stake_limits.append(
                self._contracts_to_amount(pair, limits["cost"][limit]) * stoploss_reserve
            )

        if limits["amount"][limit] is not None:
            stake_limits.append(
                self._contracts_to_amount(pair, limits["amount"][limit]) * price * margin_reserve
            )

        if not stake_limits:
            return None if isMin else float("inf")

        # The value returned should satisfy both limits: for amount (base currency) and
        # for cost (quote, stake currency), so max() is used here.
        # See also #2575 at github.
        return self._get_stake_amount_considering_leverage(
            max(stake_limits) if isMin else min(stake_limits), leverage or 1.0
        )

    def _get_stake_amount_considering_leverage(self, stake_amount: float, leverage: float) -> float:
        """
        Takes the minimum stake amount for a pair with no leverage and returns the minimum
        stake amount when leverage is considered
        :param stake_amount: The stake amount for a pair before leverage is considered
        :param leverage: The amount of leverage being used on the current trade
        """
        return stake_amount / leverage

    def get_proxy_coin(self) -> str:
        """
        Get the proxy coin for the given coin
        Falls back to the stake currency if no proxy coin is found
        :return: Proxy coin or stake currency
        """
        return self._config["stake_currency"]

    @retrier
    def get_fee(
        self,
        symbol: str,
        order_type: str = "",
        side: str = "",
        amount: float = 1,
        price: float = 1,
        taker_or_maker: MakerTaker = "maker",
    ) -> float:
        """
        Retrieve fee from exchange
        :param symbol: Pair
        :param order_type: Type of order (market, limit, ...)
        :param side: Side of order (buy, sell)
        :param amount: Amount of order
        :param price: Price of order
        :param taker_or_maker: 'maker' or 'taker' (ignored if "type" is provided)
        """
        if order_type and order_type == "market":
            taker_or_maker = "taker"
        try:
            if self._config["dry_run"] and self._config.get("fee", None) is not None:
                return self._config["fee"]
            # validate that markets are loaded before trying to get fee
            if self._api.markets is None or len(self._api.markets) == 0:
                self._api.load_markets(params={})

            return self._api.calculate_fee(
                symbol=symbol,
                type=order_type,
                side=side,
                amount=amount,
                price=price,
                takerOrMaker=taker_or_maker,
            )["rate"]
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Could not get fee info due to {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    @retrier
    def get_leverage_tiers(self) -> dict[str, list[dict]]:
        try:
            return self._api.fetch_leverage_tiers()
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Could not load leverage tiers due to {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    def load_leverage_tiers(self) -> dict[str, list[dict]]:
        if self.trading_mode == TradingMode.FUTURES:
            if self.exchange_has("fetchLeverageTiers"):
                # Fetch all leverage tiers at once
                return self.get_leverage_tiers()
        return {}

    def cache_leverage_tiers(self, tiers: dict[str, list[dict]], stake_currency: str) -> None:
        filename = self._config["datadir"] / "futures" / f"leverage_tiers_{stake_currency}.json"
        if not filename.parent.is_dir():
            filename.parent.mkdir(parents=True)
        data = {
            "updated": datetime.now(UTC),
            "data": tiers,
        }
        file_dump_json(filename, data)

    def load_cached_leverage_tiers(
        self, stake_currency: str, cache_time: timedelta | None = None
    ) -> dict[str, list[dict]] | None:
        """
        Load cached leverage tiers from disk
        :param cache_time: The maximum age of the cache before it is considered outdated
        """
        if not cache_time:
            # Default to 4 weeks
            cache_time = timedelta(weeks=4)
        filename = self._config["datadir"] / "futures" / f"leverage_tiers_{stake_currency}.json"
        if filename.is_file():
            try:
                tiers = file_load_json(filename)
                updated = tiers.get("updated")
                if updated:
                    updated_dt = parser.parse(updated)
                    if updated_dt < datetime.now(UTC) - cache_time:
                        logger.info("Cached leverage tiers are outdated. Will update.")
                        return None
                return tiers.get("data")
            except Exception:
                logger.exception("Error loading cached leverage tiers. Refreshing.")
        return None

    def fill_leverage_tiers(self) -> None:
        """
        Assigns property _leverage_tiers to a dictionary of information about the leverage
        allowed on each pair
        """
        leverage_tiers = self.load_leverage_tiers()
        for pair, tiers in leverage_tiers.items():
            pair_tiers = []
            for tier in tiers:
                pair_tiers.append(self.parse_leverage_tier(tier))
            self._leverage_tiers[pair] = pair_tiers

    def parse_leverage_tier(self, tier) -> LeverageTier:
        info = tier.get("info", {})
        return {
            "minNotional": tier["minNotional"],
            "maxNotional": tier["maxNotional"],
            "maintenanceMarginRate": tier["maintenanceMarginRate"],
            "maxLeverage": tier["maxLeverage"],
            "maintAmt": float(info["cum"]) if "cum" in info else None,
        }

    def get_max_leverage(self, pair: str, stake_amount: float | None) -> float:
        """
        Returns the maximum leverage that a pair can be traded at
        :param pair: The base/quote currency pair being traded
        :stake_amount: The total value of the traders margin_mode in quote currency
        """

        if self.trading_mode == TradingMode.SPOT:
            return 1.0

        if self.trading_mode == TradingMode.FUTURES:
            # Checks and edge cases
            if stake_amount is None:
                raise OperationalException(
                    f"{self.name}.get_max_leverage requires argument stake_amount"
                )

            if pair not in self._leverage_tiers:
                # Maybe raise exception because it can't be traded on futures?
                return 1.0

            pair_tiers = self._leverage_tiers[pair]

            if stake_amount == 0:
                return pair_tiers[0]["maxLeverage"]  # Max lev for lowest amount

            # Find the appropriate tier based on stake_amount
            prior_max_lev = None
            for tier in pair_tiers:
                # Adjust notional by leverage to do a proper comparison
                min_stake = tier["minNotional"] / (prior_max_lev or tier["maxLeverage"])
                max_stake = (
                    tier["maxNotional"] / tier["maxLeverage"]
                    if tier["maxNotional"] is not None
                    else float("inf")
                )
                prior_max_lev = tier["maxLeverage"]
                if min_stake <= stake_amount <= max_stake:
                    return tier["maxLeverage"]
                if stake_amount < min_stake and stake_amount <= max_stake:
                    # TODO: Remove this warning eventually
                    # Code could be simplified by removing the check for min-stake in the above
                    # condition, making this branch unnecessary.
                    logger.warning(
                        f"Fallback to next higher leverage tier for {pair}, stake: {stake_amount}, "
                        f"min_stake: {min_stake}."
                    )
                    return tier["maxLeverage"]

            #     else:  # if on the last tier
            if stake_amount > max_stake:
                # If stake is > than max tradeable amount
                raise InvalidOrderException(f"Stake amount {stake_amount} too high for {pair}")

            raise OperationalException(
                f"Looped through all tiers without finding a max leverage for {pair}. "
                "Should never be reached."
            )

        elif self.trading_mode == TradingMode.MARGIN:  # Search markets.limits for max lev
            market = self.markets[pair]
            if market["limits"]["leverage"]["max"] is not None:
                return market["limits"]["leverage"]["max"]
            else:
                return 1.0  # Default if max leverage cannot be found
        else:
            return 1.0

    def _get_max_notional_from_tiers(self, pair: str, leverage: float) -> float | None:
        """
        get max_notional from leverage_tiers
        :param pair: The base/quote currency pair being traded
        :param leverage: The leverage to be used
        :return: The maximum notional value for the given leverage or None if not found
        """
        if self.trading_mode != TradingMode.FUTURES:
            return None
        if pair not in self._leverage_tiers:
            return None
        pair_tiers = self._leverage_tiers[pair]
        for tier in reversed(pair_tiers):
            if leverage <= tier["maxLeverage"]:
                return tier["maxNotional"]
        return None

    def get_interest_rate(self) -> float:
        """
        Retrieve interest rate - necessary for Margin trading.
        Should not call the exchange directly when used from backtesting.
        """
        return 0.0

    def funding_fee_cutoff(self, open_date: datetime) -> bool:
        """
        Funding fees are only charged at full hours (usually every 4-8h).
        Therefore a trade opening at 10:00:01 will not be charged a funding fee until the next hour.
        :param open_date: The open date for a trade
        :return: True if the date falls on a full hour, False otherwise
        """
        return open_date.minute == 0 and open_date.second == 0

    @staticmethod
    def combine_funding_and_mark(
        funding_rates: DataFrame, mark_rates: DataFrame, futures_funding_rate: int | None = None
    ) -> DataFrame:
        """
        Combine funding-rates and mark-rates dataframes
        :param funding_rates: Dataframe containing Funding rates (Type FUNDING_RATE)
        :param mark_rates: Dataframe containing Mark rates (Type mark_ohlcv_price)
        :param futures_funding_rate: Fake funding rate to use if funding_rates are not available
        """
        relevant_cols = ["date", "open_mark", "open_fund"]
        if futures_funding_rate is None:
            return mark_rates.merge(
                funding_rates, on="date", how="inner", suffixes=["_mark", "_fund"]
            )[relevant_cols]
        else:
            if len(funding_rates) == 0:
                # No funding rate candles - full fillup with fallback variable
                mark_rates["open_fund"] = futures_funding_rate
                return mark_rates.rename(
                    columns={
                        "open": "open_mark",
                        "close": "close_mark",
                        "high": "high_mark",
                        "low": "low_mark",
                        "volume": "volume_mark",
                    }
                )[relevant_cols]

            else:
                # Fill up missing funding_rate candles with fallback value
                combined = mark_rates.merge(
                    funding_rates, on="date", how="left", suffixes=["_mark", "_fund"]
                )
                # Fill only leading missing funding rates so gaps stay untouched
                first_valid_idx = combined["open_fund"].first_valid_index()
                if first_valid_idx is None:
                    combined["open_fund"] = futures_funding_rate
                else:
                    is_leading_na = (combined.index <= first_valid_idx) & combined[
                        "open_fund"
                    ].isna()
                    combined.loc[is_leading_na, "open_fund"] = futures_funding_rate
                return combined[relevant_cols].dropna()

    def calculate_funding_fees(
        self,
        df: DataFrame,
        amount: float,
        is_short: bool,
        open_date: datetime,
        close_date: datetime,
    ) -> float:
        """
        calculates the sum of all funding fees that occurred for a pair during a futures trade
        :param df: Dataframe containing combined funding and mark rates
                   as `open_fund` and `open_mark`.
        :param amount: The quantity of the trade
        :param is_short: trade direction
        :param open_date: The date and time that the trade started
        :param close_date: The date and time that the trade ended
        """
        fees: float = 0

        if not df.empty:
            df1 = df[(df["date"] >= open_date) & (df["date"] <= close_date)]
            fees = sum(df1["open_fund"] * df1["open_mark"] * amount)
        if isnan(fees):
            fees = 0.0
        # Negate fees for longs as funding_fees expects it this way based on live endpoints.
        return fees if is_short else -fees

    def get_liquidation_price(
        self,
        pair: str,
        open_rate: float,  # Entry price of position
        is_short: bool,
        amount: float,  # Absolute value of position size
        stake_amount: float,
        leverage: float,
        wallet_balance: float,
        open_trades: list | None = None,
    ) -> float | None:
        """
        Calculate liquidation price for a pair (backtest-only, uses dry_run_liquidation_price).
        """
        if self.trading_mode == TradingMode.SPOT:
            return None
        elif self.trading_mode != TradingMode.FUTURES:
            raise OperationalException(
                f"{self.name} does not support {self.margin_mode} {self.trading_mode}"
            )

        liquidation_price = self.dry_run_liquidation_price(
            pair=pair,
            open_rate=open_rate,
            is_short=is_short,
            amount=amount,
            leverage=leverage,
            stake_amount=stake_amount,
            wallet_balance=wallet_balance,
            open_trades=open_trades or [],
        )

        if liquidation_price is not None:
            buffer_amount = abs(open_rate - liquidation_price) * self.liquidation_buffer
            liquidation_price_buffer = (
                liquidation_price - buffer_amount if is_short else liquidation_price + buffer_amount
            )
            return max(liquidation_price_buffer, 0.0)
        else:
            return None

    def dry_run_liquidation_price(
        self,
        pair: str,
        open_rate: float,
        is_short: bool,
        amount: float,
        stake_amount: float,
        leverage: float,
        wallet_balance: float,
        open_trades: list,
    ) -> float | None:
        """
        Important: Must be fetching data from cached values as this is used by backtesting!
        PERPETUAL:
         gate: https://www.gate.io/help/futures/futures/27724/liquidation-price-bankruptcy-price
         > Liquidation Price = (Entry Price ± Margin / Contract Multiplier / Size) /
                                [ 1 ± (Maintenance Margin Ratio + Taker Rate)]
            Wherein, "+" or "-" depends on whether the contract goes long or short:
            "-" for long, and "+" for short.

         okx: https://www.okx.com/support/hc/en-us/articles/
            360053909592-VI-Introduction-to-the-isolated-mode-of-Single-Multi-currency-Portfolio-margin

        :param pair: Pair to calculate liquidation price for
        :param open_rate: Entry price of position
        :param is_short: True if the trade is a short, false otherwise
        :param amount: Absolute value of position size incl. leverage (in base currency)
        :param stake_amount: Stake amount - Collateral in settle currency.
        :param leverage: Leverage used for this position.
        :param wallet_balance: Amount of margin_mode in the wallet being used to trade
            Cross-Margin Mode: crossWalletBalance
            Isolated-Margin Mode: isolatedWalletBalance
        :param open_trades: List of other open trades in the same wallet
        """

        market = self.markets[pair]
        # default to some default fee if not available from exchange
        taker_fee_rate = market["taker"] or self._api.describe().get("fees", {}).get(
            "trading", {}
        ).get("taker", 0.001)
        mm_ratio, _ = self.get_maintenance_ratio_and_amt(pair, stake_amount)

        if self.trading_mode == TradingMode.FUTURES and self.margin_mode == MarginMode.ISOLATED:
            if market["inverse"]:
                raise OperationalException("Freqtrade does not yet support inverse contracts")

            value = wallet_balance / amount

            mm_ratio_taker = mm_ratio + taker_fee_rate
            if is_short:
                return (open_rate + value) / (1 + mm_ratio_taker)
            else:
                return (open_rate - value) / (1 - mm_ratio_taker)
        else:
            raise OperationalException(
                "Freqtrade only supports isolated futures for leverage trading"
            )

    def get_maintenance_ratio_and_amt(
        self,
        pair: str,
        notional_value: float,
    ) -> tuple[float, float | None]:
        """
        Important: Must be fetching data from cached values as this is used by backtesting!
        :param pair: Market symbol
        :param notional_value: The total trade amount in quote currency
        :return: (maintenance margin ratio, maintenance amount)
        """

        if (
            self._config.get("runmode") in OPTIMIZE_MODES
            or self.exchange_has("fetchLeverageTiers")
            or self.exchange_has("fetchMarketLeverageTiers")
        ):
            if pair not in self._leverage_tiers:
                raise InvalidOrderException(
                    f"Maintenance margin rate for {pair} is unavailable for {self.name}"
                )

            pair_tiers = self._leverage_tiers[pair]

            for tier in reversed(pair_tiers):
                if notional_value >= tier["minNotional"]:
                    return (tier["maintenanceMarginRate"], tier["maintAmt"])

            raise ExchangeError("nominal value can not be lower than 0")
            # The lowest notional_floor for any pair in fetch_leverage_tiers is always 0 because it
            # describes the min amt for a tier, and the lowest tier will always go down to 0
        else:
            raise ExchangeError(f"Cannot get maintenance ratio using {self.name}")
