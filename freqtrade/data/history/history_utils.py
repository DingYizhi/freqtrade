import logging
import operator
from datetime import datetime
from pathlib import Path

import polars as pl

from freqtrade.configuration import TimeRange
from freqtrade.data.converter import clean_ohlcv_dataframe
from freqtrade.data.history.datahandlers import IDataHandler, get_datahandler
from freqtrade.enums import CandleType
from freqtrade.exceptions import OperationalException


logger = logging.getLogger(__name__)


def load_pair_history(
    pair: str,
    timeframe: str,
    datadir: Path,
    *,
    timerange: TimeRange | None = None,
    fill_up_missing: bool = True,
    drop_incomplete: bool = False,
    startup_candles: int = 0,
    data_format: str | None = None,
    data_handler: IDataHandler | None = None,
    candle_type: CandleType = CandleType.SPOT,
) -> pl.DataFrame:
    """
    Load cached ohlcv history for the given pair.

    :param pair: Pair to load data for
    :param timeframe: Timeframe (e.g. "5m")
    :param datadir: Path to the data storage location.
    :param data_format: Format of the data. Ignored if data_handler is set.
    :param timerange: Limit data to be loaded to this timerange
    :param fill_up_missing: Fill missing values with "No action"-candles
    :param drop_incomplete: Drop last candle assuming it may be incomplete.
    :param startup_candles: Additional candles to load at the start of the period
    :param data_handler: Initialized data-handler to use.
    :param candle_type: Any of the enum CandleType (must match trading mode!)
    :return: polars DataFrame with ohlcv data, or empty DataFrame
    """
    data_handler = get_datahandler(datadir, data_format, data_handler)

    return data_handler.ohlcv_load(
        pair=pair,
        timeframe=timeframe,
        timerange=timerange,
        fill_missing=fill_up_missing,
        drop_incomplete=drop_incomplete,
        startup_candles=startup_candles,
        candle_type=candle_type,
    )


def load_data(
    datadir: Path,
    timeframe: str,
    pairs: list[str],
    *,
    timerange: TimeRange | None = None,
    fill_up_missing: bool = True,
    startup_candles: int = 0,
    fail_without_data: bool = False,
    data_format: str = "feather",
    candle_type: CandleType = CandleType.SPOT,
    user_futures_funding_rate: int | None = None,
) -> dict[str, pl.DataFrame]:
    """
    Load ohlcv history data for a list of pairs.

    :param datadir: Path to the data storage location.
    :param timeframe: Timeframe (e.g. "5m")
    :param pairs: List of pairs to load
    :param timerange: Limit data to be loaded to this timerange
    :param fill_up_missing: Fill missing values with "No action"-candles
    :param startup_candles: Additional candles to load at the start of the period
    :param fail_without_data: Raise OperationalException if no data is found.
    :param data_format: Data format which should be used. Defaults to feather
    :param candle_type: Any of the enum CandleType (must match trading mode!)
    :return: dict(<pair>:<polars DataFrame>)
    """
    result: dict[str, pl.DataFrame] = {}
    if startup_candles > 0 and timerange:
        logger.debug(f"Using indicator startup period: {startup_candles} ...")

    data_handler = get_datahandler(datadir, data_format)

    for pair in pairs:
        hist = load_pair_history(
            pair=pair,
            timeframe=timeframe,
            datadir=datadir,
            timerange=timerange,
            fill_up_missing=fill_up_missing,
            startup_candles=startup_candles,
            data_handler=data_handler,
            candle_type=candle_type,
        )
        if not hist.is_empty():
            result[pair] = hist
        else:
            if candle_type is CandleType.FUNDING_RATE and user_futures_funding_rate is not None:
                logger.warning(f"{pair} using user specified [{user_futures_funding_rate}]")
            elif candle_type not in (CandleType.SPOT, CandleType.FUTURES):
                result[pair] = pl.DataFrame(schema={"date": pl.Datetime("us", "UTC"), "open": pl.Float64, "close": pl.Float64, "high": pl.Float64, "low": pl.Float64, "volume": pl.Float64})

    if fail_without_data and not result:
        raise OperationalException("No data found. Terminating.")
    return result


def get_timerange(data: dict[str, pl.DataFrame]) -> tuple[datetime, datetime]:
    """
    Get the maximum common timerange for the given backtest data.

    :param data: dictionary with preprocessed backtesting data (polars DataFrames)
    :return: tuple containing min_date, max_date
    """
    timeranges = []
    for frame in data.values():
        min_dt = frame["date"].min()
        max_dt = frame["date"].max()
        timeranges.append((min_dt, max_dt))
    return (
        min(timeranges, key=operator.itemgetter(0))[0],
        max(timeranges, key=operator.itemgetter(1))[1],
    )


def validate_backtest_data(
    data: pl.DataFrame, pair: str, min_date: datetime, max_date: datetime, timeframe_min: int
) -> bool:
    """
    Validates preprocessed backtesting data for missing values and shows warnings about it that.

    :param data: preprocessed backtesting data (polars DataFrame)
    :param pair: pair used for log output.
    :param min_date: start-date of the data
    :param max_date: end-date of the data
    :param timeframe_min: Timeframe in minutes
    """
    # total difference in minutes / timeframe-minutes
    expected_frames = int((max_date - min_date).total_seconds() // 60 // timeframe_min)
    found_missing = False
    dflen = len(data)
    if dflen < expected_frames:
        found_missing = True
        logger.warning(
            "%s has missing frames: expected %s, got %s, that's %s missing values",
            pair,
            expected_frames,
            dflen,
            expected_frames - dflen,
        )
    return found_missing
