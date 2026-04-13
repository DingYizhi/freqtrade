"""
Functions to convert data from one format to another
"""

import logging
from pathlib import Path

import polars as pl

from freqtrade.configuration import TimeRange
from freqtrade.constants import (
    DEFAULT_DATAFRAME_COLUMNS,
    DEFAULT_TRADES_COLUMNS,
    TRADES_DTYPES,
    Config,
    TradeList,
)
from freqtrade.enums import CandleType, TradingMode
from freqtrade.exceptions import OperationalException


logger = logging.getLogger(__name__)


def trades_df_remove_duplicates(trades: pl.DataFrame) -> pl.DataFrame:
    """
    Removes duplicates from the trades DataFrame.
    :param trades: DataFrame with the columns constants.DEFAULT_TRADES_COLUMNS
    :return: DataFrame with duplicates removed based on the 'timestamp' and 'id' columns
    """
    return trades.unique(subset=["timestamp", "id"], keep="first", maintain_order=True)


def trades_dict_to_list(trades: list[dict]) -> TradeList:
    """
    Convert fetch_trades result into a List (to be more memory efficient).
    :param trades: List of trades, as returned by ccxt.fetch_trades.
    :return: List of Lists, with constants.DEFAULT_TRADES_COLUMNS as columns
    """
    return [[t[col] for col in DEFAULT_TRADES_COLUMNS] for t in trades]


def trades_convert_types(trades: pl.DataFrame) -> pl.DataFrame:
    """
    Convert Trades dtypes and add 'date' column
    """
    trades = trades.with_columns(
        pl.from_epoch(pl.col("timestamp"), time_unit="ms")
        .dt.replace_time_zone("UTC")
        .alias("date")
    )
    return trades


def trades_list_to_df(trades: TradeList, convert: bool = True) -> pl.DataFrame:
    """
    convert trades list to dataframe
    :param trades: List of Lists with constants.DEFAULT_TRADES_COLUMNS as columns
    """
    if not trades:
        df = pl.DataFrame(schema={c: pl.Utf8 for c in DEFAULT_TRADES_COLUMNS})
    else:
        df = pl.DataFrame(trades, schema=DEFAULT_TRADES_COLUMNS, orient="row")

    if convert:
        df = trades_convert_types(df)

    return df


def trades_to_ohlcv(trades: pl.DataFrame, timeframe: str) -> pl.DataFrame:
    """
    Converts trades list to OHLCV list
    :param trades: polars DataFrame of trades
    :param timeframe: Timeframe to resample data to
    :return: OHLCV polars DataFrame
    :raises: ValueError if no trades are provided
    """
    from freqtrade.exchange import timeframe_to_seconds
    from freqtrade.data.converter.converter import _seconds_to_polars_duration

    if trades.is_empty():
        raise ValueError("Trade-list empty.")

    every = _seconds_to_polars_duration(timeframe_to_seconds(timeframe))
    df = trades.sort("date").group_by_dynamic("date", every=every).agg(
        pl.col("price").first().alias("open"),
        pl.col("price").max().alias("high"),
        pl.col("price").min().alias("low"),
        pl.col("price").last().alias("close"),
        pl.col("amount").sum().alias("volume"),
    )
    # Drop rows with nulls (equivalent to dropna)
    df = df.drop_nulls()
    return df.select(DEFAULT_DATAFRAME_COLUMNS)


def convert_trades_to_ohlcv(
    pairs: list[str],
    timeframes: list[str],
    datadir: Path,
    timerange: TimeRange,
    erase: bool,
    data_format_ohlcv: str,
    data_format_trades: str,
    candle_type: CandleType,
) -> None:
    """
    Convert stored trades data to ohlcv data
    """
    from freqtrade.data.history import get_datahandler

    data_handler_trades = get_datahandler(datadir, data_format=data_format_trades)
    data_handler_ohlcv = get_datahandler(datadir, data_format=data_format_ohlcv)

    logger.info(
        f"About to convert pairs: '{', '.join(pairs)}', "
        f"intervals: '{', '.join(timeframes)}' to {datadir}"
    )
    trading_mode = TradingMode.FUTURES if candle_type != CandleType.SPOT else TradingMode.SPOT
    for pair in pairs:
        trades = data_handler_trades.trades_load(pair, trading_mode)
        for timeframe in timeframes:
            if erase:
                if data_handler_ohlcv.ohlcv_purge(pair, timeframe, candle_type=candle_type):
                    logger.info(f"Deleting existing data for pair {pair}, interval {timeframe}.")
            try:
                ohlcv = trades_to_ohlcv(trades, timeframe)
                # Store ohlcv
                data_handler_ohlcv.ohlcv_store(pair, timeframe, data=ohlcv, candle_type=candle_type)
            except ValueError:
                logger.warning(f"Could not convert {pair} to OHLCV.")


def convert_trades_format(config: Config, convert_from: str, convert_to: str, erase: bool):
    """
    Convert trades from one format to another format.
    :param config: Config dictionary
    :param convert_from: Source format
    :param convert_to: Target format
    :param erase: Erase source data (does not apply if source and target format are identical)
    """
    if convert_from == "kraken_csv":
        if config["exchange"]["name"] != "kraken":
            raise OperationalException(
                "Converting from csv is only supported for kraken."
                "Please refer to the documentation for details about this special mode."
            )
        from freqtrade.data.converter.trade_converter_kraken import import_kraken_trades_from_csv

        import_kraken_trades_from_csv(config, convert_to)
        return

    from freqtrade.data.history import get_datahandler

    src = get_datahandler(config["datadir"], convert_from)
    trg = get_datahandler(config["datadir"], convert_to)

    if "pairs" not in config:
        config["pairs"] = src.trades_get_pairs(config["datadir"])
    logger.info(f"Converting trades for {config['pairs']}")
    trading_mode: TradingMode = config.get("trading_mode", TradingMode.SPOT)
    for pair in config["pairs"]:
        data = src.trades_load(pair, trading_mode)
        logger.info(f"Converting {len(data)} trades for {pair}")
        trg.trades_store(pair, data, trading_mode)

        if erase and convert_from != convert_to:
            logger.info(f"Deleting source Trade data for {pair}.")
            src.trades_purge(pair, trading_mode)
