"""
Dataprovider
Responsible to provide data to the bot
including ticker and orderbook data, live and historical candle (OHLCV) data
Common Interface for bot and strategy to access data.
"""

import logging
from collections import deque
from datetime import UTC, datetime
from typing import Any

import polars as pl

from freqtrade.configuration import TimeRange
from freqtrade.constants import Config, ListPairsWithTimeframes, PairWithTimeframe
from freqtrade.data.history import get_datahandler, load_pair_history
from freqtrade.enums import CandleType, RunMode, TradingMode
from freqtrade.exceptions import ExchangeError, OperationalException
from freqtrade.exchange import Exchange, timeframe_to_prev_date, timeframe_to_seconds
from freqtrade.exchange.exchange_types import FundingRate, OrderBook
from freqtrade.util import PeriodicCache


logger = logging.getLogger(__name__)

NO_EXCHANGE_EXCEPTION = "Exchange is not available to DataProvider."
MAX_DATAFRAME_CANDLES = 1000


class DataProvider:
    def __init__(
        self,
        config: Config,
        exchange: Exchange | None,
        pairlists=None,
        rpc: Any | None = None,
    ) -> None:
        self._config = config
        self._exchange = exchange
        self._pairlists = pairlists
        self.__rpc = rpc
        self.__cached_pairs: dict[PairWithTimeframe, tuple[pl.DataFrame, datetime]] = {}
        self.__slice_index: dict[str, int] = {}
        self.__slice_date: datetime | None = None

        self.__cached_pairs_backtesting: dict[PairWithTimeframe, pl.DataFrame] = {}
        # Cache for get_current_candle: {(pair, tf, candle_type): (slice_index, row_dict)}
        self.__current_row_cache: dict[PairWithTimeframe, tuple[int, dict]] = {}
        self._msg_queue: deque = deque()

        self._default_candle_type = self._config.get("candle_type_def", CandleType.SPOT)
        self._default_timeframe = self._config.get("timeframe", "1h")

        self.__msg_cache = PeriodicCache(
            maxsize=1000, ttl=timeframe_to_seconds(self._default_timeframe)
        )

    def _set_dataframe_max_index(self, pair: str, limit_index: int):
        """
        Limit analyzed dataframe to max specified index.
        Only relevant in backtesting.
        :param limit_index: dataframe index.
        """
        self.__slice_index[pair] = limit_index

    def _set_dataframe_max_date(self, limit_date: datetime):
        """
        Limit informative dataframe to max specified index.
        Only relevant in backtesting.
        :param limit_date: "current date"
        """
        self.__slice_date = limit_date

    def _set_cached_df(
        self, pair: str, timeframe: str, dataframe: pl.DataFrame, candle_type: CandleType
    ) -> None:
        """
        Store cached Dataframe.
        Using private method as this should never be used by a user
        (but the class is exposed via `self.dp` to the strategy)
        :param pair: pair to get the data for
        :param timeframe: Timeframe to get data for
        :param dataframe: analyzed dataframe (polars)
        :param candle_type: Any of the enum CandleType (must match trading mode!)
        """
        pair_key = (pair, timeframe, candle_type)
        self.__cached_pairs[pair_key] = (dataframe, datetime.now(UTC))

    def _emit_df(self, pair_key: PairWithTimeframe, dataframe: pl.DataFrame, new_candle: bool) -> None:
        return None

    def add_pairlisthandler(self, pairlists) -> None:
        """
        Allow adding pairlisthandler after initialization
        """
        self._pairlists = pairlists

    def historic_ohlcv(self, pair: str, timeframe: str, candle_type: str = "") -> pl.DataFrame:
        """
        Get stored historical candle (OHLCV) data
        :param pair: pair to get the data for
        :param timeframe: timeframe to get data for
        :param candle_type: '', mark, index, premiumIndex, or funding_rate
        """
        _candle_type = (
            CandleType.from_string(candle_type)
            if candle_type != ""
            else self._config["candle_type_def"]
        )
        saved_pair: PairWithTimeframe = (pair, str(timeframe), _candle_type)
        if saved_pair not in self.__cached_pairs_backtesting:
            timerange = TimeRange.parse_timerange(
                None
                if self._config.get("timerange") is None
                else str(self._config.get("timerange"))
            )

            startup_candles = self.get_required_startup(str(timeframe))
            tf_seconds = timeframe_to_seconds(str(timeframe))
            timerange.subtract_start(tf_seconds * startup_candles)

            logger.info(
                f"Loading data for {pair} {timeframe} "
                f"from {timerange.start_fmt} to {timerange.stop_fmt}"
            )

            self.__cached_pairs_backtesting[saved_pair] = load_pair_history(
                pair=pair,
                timeframe=timeframe,
                datadir=self._config["datadir"],
                timerange=timerange,
                data_format=self._config["dataformat_ohlcv"],
                candle_type=_candle_type,
            )
        return self.__cached_pairs_backtesting[saved_pair].clone()

    def get_required_startup(self, timeframe: str) -> int:
        return self._config.get("startup_candle_count", 0)

    def __fix_funding_rate_timeframe(
        self, pair: str, timeframe: str | None, candle_type: str
    ) -> str | None:
        if (
            candle_type == CandleType.FUNDING_RATE
            and (ff_tf := self.get_funding_rate_timeframe()) != timeframe
        ):
            logger.warning(
                f"{pair}, {timeframe} requested - funding rate timeframe not matching {ff_tf}."
            )
            return ff_tf

        return timeframe

    def get_pair_dataframe(
        self, pair: str, timeframe: str | None = None, candle_type: str = ""
    ) -> pl.DataFrame:
        """
        Return pair candle (OHLCV) data, either live or cached historical -- depending
        on the runmode.
        Only combinations in the pairlist or which have been specified as informative pairs
        will be available.
        :param pair: pair to get the data for
        :param timeframe: timeframe to get data for
        :return: polars DataFrame for this pair
        :param candle_type: '', mark, index, premiumIndex, or funding_rate
        """
        timeframe = self.__fix_funding_rate_timeframe(pair, timeframe, candle_type)
        if self.runmode in (RunMode.DRY_RUN, RunMode.LIVE):
            # Get live OHLCV data.
            data = self.ohlcv(pair=pair, timeframe=timeframe, candle_type=candle_type)
        else:
            # Get historical OHLCV data (cached on disk).
            timeframe = timeframe or self._config["timeframe"]
            data = self.historic_ohlcv(pair=pair, timeframe=timeframe, candle_type=candle_type)
            # Cut date to timeframe-specific date.
            # This is necessary to prevent lookahead bias in callbacks through informative pairs.
            if self.__slice_date:
                cutoff_date = timeframe_to_prev_date(timeframe, self.__slice_date)
                data = data.filter(pl.col("date") < cutoff_date)
        if len(data) == 0:
            logger.warning(f"No data found for ({pair}, {timeframe}, {candle_type}).")
        return data

    def get_analyzed_dataframe(self, pair: str, timeframe: str) -> tuple[pl.DataFrame, datetime]:
        """
        Retrieve the analyzed dataframe. Returns the full dataframe in trade mode (live / dry),
        and the last 1000 candles (up to the time evaluated at this moment) in all other modes.
        :param pair: pair to get the data for
        :param timeframe: timeframe to get data for
        :return: Tuple of (Analyzed Dataframe (polars), lastrefreshed) for the requested pair /
            timeframe combination.
            Returns empty dataframe and Epoch 0 (1970-01-01) if no dataframe was cached.
        """
        pair_key = (pair, timeframe, self._config.get("candle_type_def", CandleType.SPOT))
        if pair_key in self.__cached_pairs:
            if self.runmode in (RunMode.DRY_RUN, RunMode.LIVE):
                df, date = self.__cached_pairs[pair_key]
            else:
                df, date = self.__cached_pairs[pair_key]
                if (max_index := self.__slice_index.get(pair)) is not None:
                    start = max(0, max_index - MAX_DATAFRAME_CANDLES)
                    df = df.slice(start, max_index - start)
                else:
                    return (pl.DataFrame(), datetime.fromtimestamp(0, tz=UTC))
            return df, date
        else:
            return (pl.DataFrame(), datetime.fromtimestamp(0, tz=UTC))

    def get_current_candle(self, pair: str, timeframe: str) -> dict:
        """
        Return the current bar's analyzed row as a plain Python dict (backtesting only).
        Cached per slice index — safe to call multiple times per bar with zero extra cost.

        Intended for use inside callbacks (custom_exit, adjust_trade_position) that need
        indicator values for the current bar without going through
        get_analyzed_dataframe().row(-1, named=True).

        :param pair: pair to get the data for
        :param timeframe: timeframe to get data for
        :return: dict of column → value for the current bar, or {} if unavailable
        """
        pair_key = (pair, timeframe, self._config.get("candle_type_def", CandleType.SPOT))
        idx = self.__slice_index.get(pair)
        if idx is None:
            return {}
        cached = self.__current_row_cache.get(pair_key)
        if cached is not None and cached[0] == idx:
            return cached[1]
        # Cache miss — compute from full cached df at current slice index
        entry = self.__cached_pairs.get(pair_key)
        if entry is None:
            return {}
        df = entry[0]
        start = max(0, idx - MAX_DATAFRAME_CANDLES)
        length = idx - start
        if length <= 0:
            return {}
        row = df.slice(start, length).row(-1, named=True)
        self.__current_row_cache[pair_key] = (idx, row)
        return row

    @property
    def runmode(self) -> RunMode:
        """
        Get runmode of the bot
        can be "live", "dry-run", "backtest", "hyperopt" or "other".
        """
        return RunMode(self._config.get("runmode", RunMode.OTHER))

    def current_whitelist(self) -> list[str]:
        """
        fetch latest available whitelist.

        Useful when you have a large whitelist and need to call each pair as an informative pair.
        As available pairs does not show whitelist until after informative pairs have been cached.
        :return: list of pairs in whitelist
        """

        if self._pairlists:
            return self._pairlists.whitelist.copy()
        else:
            raise OperationalException("Dataprovider was not initialized with a pairlist provider.")

    def clear_cache(self):
        """
        Clear pair dataframe cache.
        """
        self.__cached_pairs = {}
        # Don't reset backtesting pairs -
        # otherwise they're reloaded each time during hyperopt due to with analyze_per_epoch
        # self.__cached_pairs_backtesting = {}
        self.__slice_index = {}
        self.__current_row_cache = {}

    # Exchange functions

    def refresh(
        self,
        pairlist: ListPairsWithTimeframes,
        helping_pairs: ListPairsWithTimeframes | None = None,
    ) -> None:
        """
        Refresh data, called with each cycle
        """
        if self._exchange is None:
            raise OperationalException(NO_EXCHANGE_EXCEPTION)
        final_pairs = (pairlist + helping_pairs) if helping_pairs else pairlist
        # refresh latest ohlcv data
        self._exchange.refresh_latest_ohlcv(final_pairs)
        # refresh latest trades data
        self.refresh_latest_trades(pairlist)

    def refresh_latest_trades(self, pairlist: ListPairsWithTimeframes) -> None:
        """
        Refresh latest trades data (if enabled in config)
        """

        use_public_trades = self._config.get("exchange", {}).get("use_public_trades", False)
        if use_public_trades:
            if self._exchange:
                self._exchange.refresh_latest_trades(pairlist)

    @property
    def available_pairs(self) -> ListPairsWithTimeframes:
        """
        Return a list of tuples containing (pair, timeframe) for which data is currently cached.
        Should be whitelist + open trades.
        """
        if self._exchange is None:
            raise OperationalException(NO_EXCHANGE_EXCEPTION)
        return list(self._exchange._klines.keys())

    def ohlcv(
        self, pair: str, timeframe: str | None = None, copy: bool = True, candle_type: str = ""
    ) -> pl.DataFrame:
        """
        Get candle (OHLCV) data for the given pair as polars DataFrame
        Please use the `available_pairs` method to verify which pairs are currently cached.
        :param pair: pair to get the data for
        :param timeframe: Timeframe to get data for
        :param candle_type: '', mark, index, premiumIndex, or funding_rate
        :param copy: copy dataframe before returning if True.
        """
        if self._exchange is None:
            raise OperationalException(NO_EXCHANGE_EXCEPTION)
        if self.runmode in (RunMode.DRY_RUN, RunMode.LIVE):
            _candle_type = (
                CandleType.from_string(candle_type)
                if candle_type != ""
                else self._config["candle_type_def"]
            )
            return self._exchange.klines(
                (pair, timeframe or self._config["timeframe"], _candle_type), copy=copy
            )
        else:
            return pl.DataFrame()

    def trades(
        self,
        pair: str,
        timeframe: str | None = None,
        copy: bool = True,
        candle_type: str = "",
        timerange: TimeRange | None = None,
    ) -> pl.DataFrame:
        """
        Get candle (TRADES) data for the given pair as polars DataFrame
        :param pair: pair to get the data for
        :param timeframe: Timeframe to get data for
        :param candle_type: '', mark, index, premiumIndex, or funding_rate
        :param copy: copy dataframe before returning if True.
        """
        if self.runmode in (RunMode.DRY_RUN, RunMode.LIVE):
            if self._exchange is None:
                raise OperationalException(NO_EXCHANGE_EXCEPTION)
            _candle_type = (
                CandleType.from_string(candle_type)
                if candle_type != ""
                else self._config["candle_type_def"]
            )
            return self._exchange.trades(
                (pair, timeframe or self._config["timeframe"], _candle_type), copy=copy
            )
        else:
            data_handler = get_datahandler(
                self._config["datadir"], data_format=self._config["dataformat_trades"]
            )
            trades_df = data_handler.trades_load(
                pair, self._config.get("trading_mode", TradingMode.SPOT), timerange=timerange
            )
            return trades_df

    def market(self, pair: str) -> dict[str, Any] | None:
        """
        Return market data for the pair
        :param pair: Pair to get the data for
        :return: Market data dict from ccxt or None if market info is not available for the pair
        """
        if self._exchange is None:
            raise OperationalException(NO_EXCHANGE_EXCEPTION)
        return self._exchange.markets.get(pair)

    def ticker(self, pair: str):
        """
        Return last ticker data from exchange
        Warning: Performs a network request - so use with common sense.
        :param pair: Pair to get the data for
        :return: Ticker dict from exchange or empty dict if ticker is not available for the pair
        """
        if self._exchange is None:
            raise OperationalException(NO_EXCHANGE_EXCEPTION)
        try:
            return self._exchange.fetch_ticker(pair)
        except ExchangeError:
            return {}

    def orderbook(self, pair: str, maximum: int) -> OrderBook:
        """
        Fetch latest l2 orderbook data
        Warning: Performs a network request - so use with common sense.
        :param pair: pair to get the data for
        :param maximum: Maximum number of orderbook entries to query
        :return: dict including bids/asks with a total of `maximum` entries.
        """
        if self._exchange is None:
            raise OperationalException(NO_EXCHANGE_EXCEPTION)
        return self._exchange.fetch_l2_order_book(pair, maximum)

    def funding_rate(self, pair: str) -> FundingRate:
        """
        Return Funding rate from the exchange
        Warning: Performs a network request - so use with common sense.
        :param pair: Pair to get the data for
        :return: Funding rate dict from exchange or empty dict if funding rate is not available
        """
        if self._exchange is None:
            raise OperationalException(NO_EXCHANGE_EXCEPTION)
        try:
            return self._exchange.fetch_funding_rate(pair)
        except ExchangeError:
            return {}

    def send_msg(self, message: str, *, always_send: bool = False) -> None:
        """
        Send custom RPC Notifications from your bot.
        Will not send any bot in modes other than Dry-run or Live.
        :param message: Message to be sent. Must be below 4096.
        :param always_send: If False, will send the message only once per candle, and suppress
                            identical messages.
                            Careful as this can end up spaming your chat.
                            Defaults to False
        """
        if self.runmode not in (RunMode.DRY_RUN, RunMode.LIVE):
            return

        if always_send or message not in self.__msg_cache:
            self._msg_queue.append(message)
        self.__msg_cache[message] = True

    def check_delisting(self, pair: str) -> datetime | None:
        """
        Check if a pair gonna be delisted on the exchange.
        Will only return datetime if the pair is gonna be delisted.
        :param pair: Pair to check
        :return: Datetime of the pair's delisting, None otherwise
        """
        if self._exchange is None:
            raise OperationalException(NO_EXCHANGE_EXCEPTION)

        try:
            return self._exchange.check_delisting_time(pair)
        except ExchangeError:
            logger.warning(f"Could not fetch market data for {pair}. Assuming no delisting.")
            return None

    def get_funding_rate_timeframe(self) -> str:
        """
        Get the funding rate timeframe from exchange options
        :return: Timeframe string
        """
        if self._exchange is None:
            raise OperationalException(NO_EXCHANGE_EXCEPTION)
        return self._exchange.get_option("funding_fee_timeframe")
