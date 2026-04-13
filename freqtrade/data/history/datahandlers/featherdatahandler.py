import logging

import polars as pl
from pyarrow import dataset

from freqtrade.configuration import TimeRange
from freqtrade.constants import DEFAULT_DATAFRAME_COLUMNS, DEFAULT_TRADES_COLUMNS
from freqtrade.enums import CandleType, TradingMode

from .idatahandler import IDataHandler


logger = logging.getLogger(__name__)

# Schema for empty OHLCV polars DataFrames returned when no data is available.
_EMPTY_OHLCV_SCHEMA = {
    "date": pl.Datetime("us", "UTC"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
}


class FeatherDataHandler(IDataHandler):
    _columns = DEFAULT_DATAFRAME_COLUMNS

    def ohlcv_store(
        self, pair: str, timeframe: str, data: pl.DataFrame, candle_type: CandleType
    ) -> None:
        """
        Store data in feather format.
        :param pair: Pair - used to generate filename
        :param timeframe: Timeframe - used to generate filename
        :param data: polars DataFrame containing OHLCV data
        :param candle_type: Any of the enum CandleType (must match trading mode!)
        :return: None
        """
        filename = self._pair_data_filename(self._datadir, pair, timeframe, candle_type)
        self.create_dir_if_needed(filename)
        data.select(self._columns).write_ipc(filename, compression="lz4")

    def _ohlcv_load(
        self, pair: str, timeframe: str, timerange: TimeRange | None, candle_type: CandleType
    ) -> pl.DataFrame:
        """
        Internal method used to load data for one pair from disk.
        Returns a polars DataFrame.
        """
        filename = self._pair_data_filename(self._datadir, pair, timeframe, candle_type=candle_type)
        if not filename.exists():
            # Fallback mode for 1M files
            filename = self._pair_data_filename(
                self._datadir, pair, timeframe, candle_type=candle_type, no_timeframe_modify=True
            )
            if not filename.exists():
                return pl.DataFrame(schema=_EMPTY_OHLCV_SCHEMA)
        try:
            pairdata = pl.read_ipc(filename, memory_map=False)
            pairdata.columns = self._columns
            pairdata = pairdata.with_columns(
                pl.col("open").cast(pl.Float64),
                pl.col("high").cast(pl.Float64),
                pl.col("low").cast(pl.Float64),
                pl.col("close").cast(pl.Float64),
                pl.col("volume").cast(pl.Float64),
            )
            return pairdata
        except Exception as e:
            logger.exception(
                f"Error loading data from {filename}. Exception: {e}. Returning empty dataframe."
            )
            return pl.DataFrame(schema=_EMPTY_OHLCV_SCHEMA)

    def ohlcv_append(
        self, pair: str, timeframe: str, data: pl.DataFrame, candle_type: CandleType
    ) -> None:
        """
        Append data to existing data structures
        """
        raise NotImplementedError()

    def _trades_store(self, pair: str, data: pl.DataFrame, trading_mode: TradingMode) -> None:
        """
        Store trades data to file
        :param pair: Pair - used for filename
        :param data: polars DataFrame containing trades
        :param trading_mode: Trading mode to use (used to determine the filename)
        """
        filename = self._pair_trades_filename(self._datadir, pair, trading_mode)
        self.create_dir_if_needed(filename)
        data.write_ipc(filename, compression="lz4")

    def trades_append(self, pair: str, data: pl.DataFrame):
        """
        Append data to existing files
        """
        raise NotImplementedError()

    def _build_arrow_time_filter(self, timerange: TimeRange | None):
        """
        Build Arrow predicate filter for timerange filtering.
        """
        if not timerange:
            return None

        start_set = bool(timerange.startts and timerange.startts > 0)
        stop_set = bool(timerange.stopts and timerange.stopts > 0)

        if not (start_set or stop_set):
            return None

        ts_field = dataset.field("timestamp")
        exprs = []

        if start_set:
            exprs.append(ts_field >= timerange.startts)
        if stop_set:
            exprs.append(ts_field <= timerange.stopts)

        if len(exprs) == 1:
            return exprs[0]
        else:
            return exprs[0] & exprs[1]

    def _trades_load(
        self, pair: str, trading_mode: TradingMode, timerange: TimeRange | None = None
    ) -> pl.DataFrame:
        """
        Load trades from feather file as polars DataFrame.
        :param pair: Load trades for this pair
        :param trading_mode: Trading mode to use (used to determine the filename)
        :param timerange: Timerange to load trades for - filters data to this range if provided
        :return: polars DataFrame containing trades
        """
        filename = self._pair_trades_filename(self._datadir, pair, trading_mode)
        if not filename.exists():
            return pl.DataFrame(schema={c: pl.Utf8 for c in DEFAULT_TRADES_COLUMNS})

        try:
            dataset_reader = dataset.dataset(filename, format="feather")
            time_filter = self._build_arrow_time_filter(timerange)

            if time_filter is not None and timerange is not None:
                table = dataset_reader.to_table(filter=time_filter)
                start_desc = timerange.startts if timerange.startts > 0 else "unbounded"
                stop_desc = timerange.stopts if timerange.stopts > 0 else "unbounded"
                logger.debug(
                    f"Loaded {len(table)} trades for {pair} "
                    f"(filtered start={start_desc}, stop={stop_desc})"
                )
            else:
                table = dataset_reader.to_table()
                logger.debug(f"Loaded {len(table)} trades for {pair} (unfiltered)")

            return pl.from_arrow(table)

        except (ImportError, AttributeError, ValueError) as e:
            # Fallback: load entire file via polars
            logger.warning(f"Unable to use Arrow filtering, loading entire trades file: {e}")
            return pl.read_ipc(filename, memory_map=False)

    @classmethod
    def _get_file_extension(cls):
        return "feather"
