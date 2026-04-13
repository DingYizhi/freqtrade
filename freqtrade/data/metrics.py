import logging
import math
from dataclasses import dataclass
from datetime import datetime

import polars as pl


logger = logging.getLogger(__name__)


def calculate_market_change(
    data: dict[str, pl.DataFrame], column: str = "close", min_date: datetime | None = None
) -> float:
    """
    Calculate market change based on "column".
    Calculation is done by taking the first non-null and the last non-null element of each column
    and calculating the pctchange as "(last - first) / first".
    Then the results per pair are combined as mean.

    :param data: Dict of Dataframes, dict key should be pair.
    :param column: Column in the original dataframes to use
    :param min_date: Minimum date to consider for calculations. Market change should only be
        calculated for data actually backtested, excluding startup periods.
    :return:
    """
    tmp_means = []
    for pair, df in data.items():
        df1 = df
        if min_date is not None:
            df1 = df1.filter(pl.col("date") >= min_date)
        if df1.is_empty():
            logger.warning(f"Pair {pair} has no data after {min_date}.")
            continue
        vals = df1[column].drop_nulls()
        start = vals[0]
        end = vals[-1]
        tmp_means.append((end - start) / start)

    if not tmp_means:
        return 0.0
    return float(sum(tmp_means) / len(tmp_means))


def combine_dataframes_by_column(
    data: dict[str, pl.DataFrame], column: str = "close"
) -> pl.DataFrame:
    """
    Combine multiple dataframes "column"
    :param data: Dict of Dataframes, dict key should be pair.
    :param column: Column in the original dataframes to use
    :return: DataFrame with date and each pair's column values.
    :raise: ValueError if no data is provided.
    """
    if not data:
        raise ValueError("No data provided.")
    frames = []
    for pair, df in data.items():
        frames.append(df.select(pl.col("date"), pl.col(column).alias(pair)))
    # Join all on date
    result = frames[0]
    for f in frames[1:]:
        result = result.join(f, on="date", how="full", coalesce=True).sort("date")
    return result


def combined_dataframes_with_rel_mean(
    data: dict[str, pl.DataFrame], fromdt: datetime, todt: datetime, column: str = "close"
) -> pl.DataFrame:
    """
    Combine multiple dataframes "column"
    :param data: Dict of Dataframes, dict key should be pair.
    :param column: Column in the original dataframes to use
    :return: DataFrame with date, mean, rel_mean, count columns.
    :raise: ValueError if no data is provided.
    """
    df_comb = combine_dataframes_by_column(data, column)
    # Trim to timeframe
    df_comb = df_comb.filter((pl.col("date") >= fromdt) & (pl.col("date") < todt))

    pair_cols = [c for c in df_comb.columns if c != "date"]

    # pct_change per column, mean across columns, cumsum
    pct_dfs = []
    for col in pair_cols:
        pct_dfs.append(
            (pl.col(col) - pl.col(col).shift(1)) / pl.col(col).shift(1)
        )
    # mean of pct changes across columns
    df_comb = df_comb.with_columns(
        pl.mean_horizontal(*[
            ((pl.col(c) - pl.col(c).shift(1)) / pl.col(c).shift(1))
            for c in pair_cols
        ]).fill_null(0.0).cum_sum().alias("rel_mean"),
        pl.mean_horizontal(*[pl.col(c) for c in pair_cols]).alias("mean"),
        pl.sum_horizontal(*[pl.col(c).is_not_null().cast(pl.Int64) for c in pair_cols]).alias("count"),
    )
    return df_comb.select("date", "mean", "rel_mean", "count")


def create_cum_profit(
    df: pl.DataFrame, trades: pl.DataFrame, col_name: str, timeframe: str
) -> pl.DataFrame:
    """
    Adds a column `col_name` with the cumulative profit for the given trades array.
    :param df: DataFrame with date column
    :param trades: DataFrame containing trades (requires columns close_date and profit_abs)
    :param col_name: Column name that will be assigned the results
    :param timeframe: Timeframe used during the operations
    :return: Returns df with one additional column, col_name, containing the cumulative profit.
    :raise: ValueError if trade-dataframe was found empty.
    """
    if len(trades) == 0:
        raise ValueError("Trade dataframe empty.")
    from freqtrade.data.converter.converter import _seconds_to_polars_duration
    from freqtrade.exchange import timeframe_to_seconds

    every = _seconds_to_polars_duration(timeframe_to_seconds(timeframe))
    # Resample trades to timeframe buckets and sum profit_abs
    _trades_sum = (
        trades.sort("close_date")
        .group_by_dynamic("close_date", every=every)
        .agg(pl.col("profit_abs").sum())
        .rename({"close_date": "date"})
    )
    # Rename to avoid collision with any existing profit_abs column
    _trades_sum = _trades_sum.rename({"profit_abs": "_cum_profit_tmp"})
    # Join onto df
    df = df.join(_trades_sum, on="date", how="left")
    # cumsum with fill
    df = df.with_columns(
        pl.col("_cum_profit_tmp").fill_null(0.0).cum_sum().alias(col_name)
    ).drop("_cum_profit_tmp")
    return df


def _calc_drawdown_series(
    profit_results: pl.DataFrame, *, date_col: str, value_col: str, starting_balance: float
) -> pl.DataFrame:
    cumulative = profit_results[value_col].cum_sum()
    high_value = cumulative.cum_max().zip_with(cumulative.cum_max() > 0, pl.Series([0.0] * len(cumulative)))
    # Proper: high_value = max(0, cummax)
    high_value_list = cumulative.cum_max().to_list()
    high_value = pl.Series("high_value", [max(0.0, v) for v in high_value_list])
    drawdown = cumulative - high_value

    dates = profit_results[date_col]

    if starting_balance:
        cumulative_balance = starting_balance + cumulative
        max_balance = starting_balance + high_value
        drawdown_relative = (max_balance - cumulative_balance) / max_balance
    else:
        drawdown_relative = (high_value - cumulative) / high_value

    max_drawdown_df = pl.DataFrame({
        "cumulative": cumulative,
        "high_value": high_value,
        "drawdown": drawdown,
        "drawdown_relative": drawdown_relative,
        "date": dates,
    })

    # Add zero row at start
    zero_row = pl.DataFrame({
        "cumulative": [0.0],
        "high_value": [0.0],
        "drawdown": [0.0],
        "drawdown_relative": [0.0],
        "date": [profit_results[date_col][0]],
    })
    max_drawdown_df = pl.concat([zero_row, max_drawdown_df])
    return max_drawdown_df


def calculate_underwater(
    trades: pl.DataFrame,
    *,
    date_col: str = "close_date",
    value_col: str = "profit_ratio",
    starting_balance: float = 0.0,
):
    """
    Calculate max drawdown and the corresponding close dates
    :param trades: DataFrame containing trades (requires columns close_date and profit_ratio)
    :param date_col: Column in DataFrame to use for dates (defaults to 'close_date')
    :param value_col: Column in DataFrame to use for values (defaults to 'profit_ratio')
    :return: max_drawdown_df
    :raise: ValueError if trade-dataframe was found empty.
    """
    if len(trades) == 0:
        raise ValueError("Trade dataframe empty.")
    profit_results = trades.sort(date_col)
    max_drawdown_df = _calc_drawdown_series(
        profit_results, date_col=date_col, value_col=value_col, starting_balance=starting_balance
    )

    return max_drawdown_df


@dataclass()
class DrawDownResult:
    # Max drawdown fields
    drawdown_abs: float = 0.0
    high_date: datetime | None = None
    low_date: datetime | None = None
    high_value: float = 0.0
    low_value: float = 0.0
    relative_account_drawdown: float = 0.0
    # Current drawdown fields
    current_high_date: datetime | None = None
    current_high_value: float = 0.0
    current_drawdown_abs: float = 0.0
    current_relative_account_drawdown: float = 0.0


def calculate_max_drawdown(
    trades: pl.DataFrame,
    *,
    date_col: str = "close_date",
    value_col: str = "profit_abs",
    starting_balance: float = 0,
    relative: bool = False,
) -> DrawDownResult:
    """
    Calculate max drawdown and current drawdown with corresponding dates
    :param trades: DataFrame containing trades (requires columns close_date and profit_abs)
    :param date_col: Column in DataFrame to use for dates (defaults to 'close_date')
    :param value_col: Column in DataFrame to use for values (defaults to 'profit_abs')
    :param starting_balance: Portfolio starting balance - properly calculate relative drawdown.
    :param relative: If True, use relative drawdown for max calculation instead of absolute
    :return: DrawDownResult object
    :raise: ValueError if trade-dataframe was found empty.
    """
    if len(trades) == 0:
        raise ValueError("Trade dataframe empty.")

    profit_results = trades.sort(date_col)
    max_drawdown_df = _calc_drawdown_series(
        profit_results, date_col=date_col, value_col=value_col, starting_balance=starting_balance
    )
    # max_drawdown_df has an extra zero row at the start

    # Calculate maximum drawdown
    if relative:
        idxmin = max_drawdown_df["drawdown_relative"].arg_max()
    else:
        idxmin = max_drawdown_df["drawdown"].arg_min()

    high_idx = max_drawdown_df["high_value"][:idxmin + 1].arg_max()
    high_date = profit_results[date_col][max(high_idx - 1, 0)]
    low_date = profit_results[date_col][max(idxmin - 1, 0)]
    high_val = max_drawdown_df["cumulative"][high_idx]
    low_val = max_drawdown_df["cumulative"][idxmin]
    max_drawdown_rel = max_drawdown_df["drawdown_relative"][idxmin]

    # Calculate current drawdown
    current_high_idx = max_drawdown_df["high_value"][:-1].arg_max()
    current_high_date = profit_results[date_col][max(current_high_idx - 1, 0)]
    current_high_value = max_drawdown_df["high_value"][-1]
    current_cumulative = max_drawdown_df["cumulative"][-1]
    current_drawdown_abs = current_high_value - current_cumulative
    current_drawdown_relative = max_drawdown_df["drawdown_relative"][-1]

    return DrawDownResult(
        # Max drawdown
        drawdown_abs=abs(max_drawdown_df["drawdown"][idxmin]),
        high_date=high_date,
        low_date=low_date,
        high_value=high_val,
        low_value=low_val,
        relative_account_drawdown=max_drawdown_rel,
        # Current drawdown
        current_high_date=current_high_date,
        current_high_value=current_high_value,
        current_drawdown_abs=current_drawdown_abs,
        current_relative_account_drawdown=current_drawdown_relative,
    )


def calculate_csum(trades: pl.DataFrame, starting_balance: float = 0) -> tuple[float, float]:
    """
    Calculate min/max cumsum of trades, to show if the wallet/stake amount ratio is sane
    :param trades: DataFrame containing trades (requires columns close_date and profit_abs)
    :param starting_balance: Add starting balance to results, to show the wallets high / low points
    :return: Tuple (float, float) with cumsum of profit_abs
    :raise: ValueError if trade-dataframe was found empty.
    """
    if len(trades) == 0:
        raise ValueError("Trade dataframe empty.")

    csum = trades["profit_abs"].cum_sum()
    csum_min = csum.min() + starting_balance
    csum_max = csum.max() + starting_balance

    return csum_min, csum_max


def calculate_cagr(days_passed: int, starting_balance: float, final_balance: float) -> float:
    """
    Calculate CAGR
    :param days_passed: Days passed between start and ending balance
    :param starting_balance: Starting balance
    :param final_balance: Final balance to calculate CAGR against
    :return: CAGR
    """
    if (final_balance < 0) or (starting_balance <= 0) or (days_passed <= 0):
        return 0
    return (final_balance / starting_balance) ** (1 / (days_passed / 365)) - 1


def calculate_expectancy(trades: pl.DataFrame) -> tuple[float, float]:
    """
    Calculate expectancy
    :param trades: DataFrame containing trades (requires columns close_date and profit_abs)
    :return: expectancy, expectancy_ratio
    """

    expectancy = 0.0
    expectancy_ratio = 100.0

    if len(trades) > 0:
        profit_abs = trades["profit_abs"]
        winning_mask = profit_abs > 0
        losing_mask = profit_abs < 0

        profit_sum = profit_abs.filter(winning_mask).sum()
        loss_sum = abs(profit_abs.filter(losing_mask).sum())
        nb_win_trades = winning_mask.sum()
        nb_loss_trades = losing_mask.sum()

        average_win = (profit_sum / nb_win_trades) if nb_win_trades > 0 else 0
        average_loss = (loss_sum / nb_loss_trades) if nb_loss_trades > 0 else 0
        winrate = nb_win_trades / len(trades)
        loserate = nb_loss_trades / len(trades)

        expectancy = (winrate * average_win) - (loserate * average_loss)
        if average_loss > 0:
            risk_reward_ratio = average_win / average_loss
            expectancy_ratio = ((1 + risk_reward_ratio) * winrate) - 1

    return expectancy, expectancy_ratio


def calculate_sortino(
    trades: pl.DataFrame,
    min_date: datetime | None,
    max_date: datetime | None,
    starting_balance: float,
) -> float:
    """
    Calculate sortino
    :param trades: DataFrame containing trades (requires columns profit_abs)
    :return: sortino
    """
    if (len(trades) == 0) or (min_date is None) or (max_date is None) or (min_date == max_date):
        return 0

    total_profit = trades["profit_abs"] / starting_balance
    days_period = max(1, (max_date - min_date).days)

    expected_returns_mean = total_profit.sum() / days_period

    down_profits = trades.filter(pl.col("profit_abs") < 0)["profit_abs"] / starting_balance
    if len(down_profits) > 0:
        down_stdev = down_profits.std()
        if down_stdev is not None and down_stdev != 0 and not math.isnan(down_stdev):
            sortino_ratio = expected_returns_mean / down_stdev * math.sqrt(365)
        else:
            sortino_ratio = -100
    else:
        sortino_ratio = -100

    return sortino_ratio


def calculate_sharpe(
    trades: pl.DataFrame,
    min_date: datetime | None,
    max_date: datetime | None,
    starting_balance: float,
) -> float:
    """
    Calculate sharpe
    :param trades: DataFrame containing trades (requires column profit_abs)
    :return: sharpe
    """
    if (len(trades) == 0) or (min_date is None) or (max_date is None) or (min_date == max_date):
        return 0

    total_profit = trades["profit_abs"] / starting_balance
    days_period = max(1, (max_date - min_date).days)

    expected_returns_mean = total_profit.sum() / days_period
    up_stdev = total_profit.std()

    if up_stdev is not None and up_stdev != 0:
        sharp_ratio = expected_returns_mean / up_stdev * math.sqrt(365)
    else:
        sharp_ratio = -100

    return sharp_ratio


def calculate_calmar(
    trades: pl.DataFrame,
    min_date: datetime | None,
    max_date: datetime | None,
    starting_balance: float,
) -> float:
    """
    Calculate calmar
    :param trades: DataFrame containing trades (requires columns close_date and profit_abs)
    :return: calmar
    """
    if (len(trades) == 0) or (min_date is None) or (max_date is None) or (min_date == max_date):
        return 0

    total_profit = trades["profit_abs"].sum() / starting_balance
    days_period = max(1, (max_date - min_date).days)

    expected_returns_mean = total_profit / days_period * 100

    # calculate max drawdown
    try:
        drawdown = calculate_max_drawdown(
            trades, value_col="profit_abs", starting_balance=starting_balance
        )
        max_drawdown = drawdown.relative_account_drawdown
    except ValueError:
        max_drawdown = 0

    if max_drawdown != 0:
        calmar_ratio = expected_returns_mean / max_drawdown * math.sqrt(365)
    else:
        calmar_ratio = -100

    return calmar_ratio


def calculate_sqn(trades: pl.DataFrame, starting_balance: float) -> float:
    """
    Calculate System Quality Number (SQN) - Van K. Tharp.
    :param trades: DataFrame containing trades (requires column profit_abs)
    :param starting_balance: Starting balance of the trading system
    :return: SQN value
    """
    if len(trades) == 0:
        return 0.0

    total_profit = trades["profit_abs"] / starting_balance
    number_of_trades = len(trades)

    average_profits = total_profit.mean()
    profits_std = total_profit.std()

    if profits_std is not None and profits_std != 0 and not math.isnan(profits_std):
        sqn = math.sqrt(number_of_trades) * (average_profits / profits_std)
    else:
        sqn = -100.0

    return round(sqn, 4)
