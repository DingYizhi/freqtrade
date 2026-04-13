from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import polars as pl

from freqtrade.enums import CandleType
from freqtrade.exceptions import OperationalException


PopulateIndicators = Callable[[Any, pl.DataFrame, dict], pl.DataFrame]


@dataclass
class InformativeData:
    asset: str | None
    timeframe: str
    fmt: str | Callable[[Any], str] | None
    ffill: bool
    candle_type: CandleType | None


def informative(
    timeframe: str,
    asset: str = "",
    fmt: str | Callable[[Any], str] | None = None,
    *,
    candle_type: CandleType | str | None = None,
    ffill: bool = True,
) -> Callable[[PopulateIndicators], PopulateIndicators]:
    """
    A decorator for populate_indicators_Nn(self, dataframe, metadata), allowing these functions to
    define informative indicators.

    Example usage:

        @informative('1h')
        def populate_indicators_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
            dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)
            return dataframe

    :param timeframe: Informative timeframe. Must always be equal or higher than strategy timeframe.
    :param asset: Informative asset, for example BTC, BTC/USDT, ETH/BTC. Do not specify to use
                  current pair. Also supports limited pair format strings (see below)
    :param fmt: Column format (str) or column formatter (callable(name, asset, timeframe)). When not
    specified, defaults to:
    * {base}_{quote}_{column}_{timeframe} if asset is specified.
    * {column}_{timeframe} if asset is not specified.
    :param ffill: ffill dataframe after merging informative pair.
    :param candle_type: '', mark, index, premiumIndex, or funding_rate
    """
    _asset = asset
    _timeframe = timeframe
    _fmt = fmt
    _ffill = ffill
    _candle_type = CandleType.from_string(candle_type) if candle_type else None

    def decorator(fn: PopulateIndicators):
        informative_pairs = getattr(fn, "_ft_informative", [])
        informative_pairs.append(InformativeData(_asset, _timeframe, _fmt, _ffill, _candle_type))
        setattr(fn, "_ft_informative", informative_pairs)  # noqa: B010
        return fn

    return decorator


def __get_pair_formats(market: dict[str, Any] | None) -> dict[str, str]:
    if not market:
        return {}
    base = market["base"]
    quote = market["quote"]
    return {
        "base": base.lower(),
        "BASE": base.upper(),
        "quote": quote.lower(),
        "QUOTE": quote.upper(),
    }


def _format_pair_name(config, pair: str, market: dict[str, Any] | None = None) -> str:
    return pair.format(
        stake_currency=config["stake_currency"],
        stake=config["stake_currency"],
        **__get_pair_formats(market),
    ).upper()


def _create_and_merge_informative_pair(
    strategy,
    dataframe: pl.DataFrame,
    metadata: dict,
    inf_data: InformativeData,
    populate_indicators_fn: PopulateIndicators,
):
    """
    Create and merge informative pair data using polars.
    This is used by the @informative decorator in strategies.
    """
    from freqtrade.exchange import timeframe_to_minutes
    from freqtrade.data.converter.converter import _seconds_to_polars_duration

    asset = inf_data.asset or ""
    timeframe = inf_data.timeframe
    timeframe1 = inf_data.timeframe
    fmt = inf_data.fmt
    candle_type = inf_data.candle_type
    if candle_type == CandleType.FUNDING_RATE:
        timeframe1 = strategy.dp.get_funding_rate_timeframe()

    config = strategy.config

    if asset:
        market1 = strategy.dp.market(metadata["pair"])
        asset = _format_pair_name(config, asset, market1)
    else:
        asset = metadata["pair"]

    market = strategy.dp.market(asset)
    if market is None:
        raise OperationalException(f"Market {asset} is not available.")

    if not fmt:
        fmt = "{column}_{timeframe}"
        if inf_data.asset:
            fmt = "{base}_{quote}_" + fmt

    inf_metadata = {"pair": asset, "timeframe": timeframe}
    inf_dataframe = strategy.dp.get_pair_dataframe(asset, timeframe1, candle_type)
    if inf_dataframe.is_empty():
        raise ValueError(
            f"Informative dataframe for ({asset}, {timeframe1}, {candle_type}) is empty. "
            "Can't populate informative indicators."
        )
    inf_dataframe = populate_indicators_fn(strategy, inf_dataframe, inf_metadata)

    formatter: Any = None
    if callable(fmt):
        formatter = fmt
    else:
        formatter = fmt.format

    fmt_args = {
        **__get_pair_formats(market),
        "asset": asset,
        "timeframe": timeframe,
    }

    # Rename informative columns
    rename_map = {col: formatter(column=col, **fmt_args) for col in inf_dataframe.columns}
    inf_dataframe = inf_dataframe.rename(rename_map)

    date_column = formatter(column="date", **fmt_args)
    if date_column in dataframe.columns:
        raise OperationalException(
            f"Duplicate column name {date_column} exists in "
            f"dataframe! Ensure column names are unique!"
        )

    # Polars merge: shift informative dates to avoid lookahead
    minutes_inf = timeframe_to_minutes(timeframe1)
    minutes = timeframe_to_minutes(strategy.timeframe)

    if minutes == minutes_inf:
        inf_dataframe = inf_dataframe.with_columns(
            pl.col(date_column).alias("_date_merge")
        )
    elif minutes < minutes_inf:
        if not inf_dataframe.is_empty():
            shift_duration = f"{minutes_inf - minutes}m"
            inf_dataframe = inf_dataframe.with_columns(
                (pl.col(date_column) + pl.duration(minutes=minutes_inf - minutes)).alias("_date_merge")
            )
        else:
            inf_dataframe = inf_dataframe.with_columns(
                pl.col(date_column).alias("_date_merge")
            )
    else:
        raise ValueError(
            "Tried to merge a faster timeframe to a slower timeframe."
            "This would create new rows, and can throw off your regular indicators."
        )

    # asof join on date
    dataframe = dataframe.sort("date")
    inf_dataframe = inf_dataframe.sort("_date_merge")

    # Drop date_column from inf before join to avoid conflicts
    join_cols = [c for c in inf_dataframe.columns if c != date_column]
    dataframe = dataframe.join_asof(
        inf_dataframe.select(join_cols),
        left_on="date",
        right_on="_date_merge",
        strategy="backward",
    ).drop("_date_merge")

    return dataframe
