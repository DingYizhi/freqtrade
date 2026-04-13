import logging
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import polars as pl

from freqtrade.constants import BACKTEST_BREAKDOWNS, DATETIME_PRINT_FORMAT
from freqtrade.data.metrics import (
    calculate_cagr,
    calculate_calmar,
    calculate_csum,
    calculate_expectancy,
    calculate_market_change,
    calculate_max_drawdown,
    calculate_sharpe,
    calculate_sortino,
    calculate_sqn,
)
from freqtrade.ft_types import (
    BacktestContentType,
    BacktestResultType,
    get_BacktestResultType_default,
)
from freqtrade.util import decimals_per_coin, fmt_coin, format_duration, get_dry_run_wallet


logger = logging.getLogger(__name__)


def generate_trade_signal_candles(
    preprocessed_df: dict[str, pl.DataFrame], bt_results: BacktestContentType, date_col: str
) -> dict[str, pl.DataFrame]:
    signal_candles_only = {}
    for pair in preprocessed_df.keys():
        pairdf = preprocessed_df[pair]
        resdf = bt_results["results"]
        pairresults = resdf.filter(pl.col("pair") == pair)

        if pairdf.height > 0:
            frames = []
            for row in pairresults.iter_rows(named=True):
                allinds = pairdf.filter(pl.col("date") < row[date_col])
                if allinds.height > 0:
                    frames.append(allinds.tail(1))

            if frames:
                signal_candles_only[pair] = pl.concat(frames)
            else:
                signal_candles_only[pair] = pl.DataFrame(schema=pairdf.schema)
    return signal_candles_only


def generate_rejected_signals(
    preprocessed_df: dict[str, pl.DataFrame], rejected_dict: dict[str, list]
) -> dict[str, pl.DataFrame]:
    rejected_candles_only = {}
    for pair, signals in rejected_dict.items():
        pairdf = preprocessed_df[pair]

        frames = []
        for t in signals:
            data_df_row = pairdf.filter(pl.col("date") == t[0])
            if data_df_row.height > 0:
                data_df_row = data_df_row.with_columns(
                    pl.lit(pair).alias("pair"),
                    pl.lit(t[1]).alias("enter_tag"),
                )
                frames.append(data_df_row)

        if frames:
            rejected_candles_only[pair] = pl.concat(frames)
        else:
            rejected_candles_only[pair] = pl.DataFrame()
    return rejected_candles_only


def _generate_result_line(
    result: pl.DataFrame,
    min_date: datetime,
    max_date: datetime,
    starting_balance: float,
    first_column: str | list[str],
) -> dict:
    """
    Generate one result dict, with "first_column" as key.
    """
    profit_abs_sum = result["profit_abs"].sum()
    profit_total = profit_abs_sum / starting_balance
    backtest_days = (max_date - min_date).days or 1
    final_balance = starting_balance + profit_abs_sum
    expectancy, expectancy_ratio = calculate_expectancy(result)
    winning_profit = result.filter(pl.col("profit_abs") > 0)["profit_abs"].sum()
    losing_profit = result.filter(pl.col("profit_abs") < 0)["profit_abs"].sum()
    profit_factor = winning_profit / abs(losing_profit) if losing_profit else 0.0

    try:
        drawdown = calculate_max_drawdown(
            result, value_col="profit_abs", starting_balance=starting_balance
        )
    except ValueError:
        drawdown = None

    n = len(result)
    return {
        "key": first_column,
        "trades": n,
        "profit_mean": result["profit_ratio"].mean() if n > 0 else 0.0,
        "profit_mean_pct": (
            round(result["profit_ratio"].mean() * 100.0, 2) if n > 0 else 0.0
        ),
        "profit_total_abs": profit_abs_sum,
        "profit_total": profit_total,
        "profit_total_pct": round(profit_total * 100.0, 2),
        "duration_avg": (
            str(timedelta(minutes=round(result["trade_duration"].mean())))
            if n > 0
            else "0:00"
        ),
        "wins": result.filter(pl.col("profit_abs") > 0).height,
        "draws": result.filter(pl.col("profit_abs") == 0).height,
        "losses": result.filter(pl.col("profit_abs") < 0).height,
        "winrate": result.filter(pl.col("profit_abs") > 0).height / n if n else 0.0,
        "cagr": calculate_cagr(backtest_days, starting_balance, final_balance),
        "expectancy": expectancy,
        "expectancy_ratio": expectancy_ratio,
        "sortino": calculate_sortino(result, min_date, max_date, starting_balance),
        "sharpe": calculate_sharpe(result, min_date, max_date, starting_balance),
        "calmar": calculate_calmar(result, min_date, max_date, starting_balance),
        "sqn": calculate_sqn(result, starting_balance),
        "profit_factor": profit_factor,
        "max_drawdown_account": drawdown.relative_account_drawdown if drawdown else 0.0,
        "max_drawdown_abs": drawdown.drawdown_abs if drawdown else 0.0,
    }


def calculate_trade_volume(trades_dict: list[dict[str, Any]]) -> float:
    return sum(sum(order["cost"] for order in trade.get("orders", [])) for trade in trades_dict)


def generate_pair_metrics(
    pairlist: list[str],
    stake_currency: str,
    starting_balance: float,
    results: pl.DataFrame,
    min_date: datetime,
    max_date: datetime,
    skip_nan: bool = False,
) -> list[dict]:
    tabular_data = []

    for pair in pairlist:
        result = results.filter(pl.col("pair") == pair)
        if skip_nan and result["profit_abs"].is_null().all():
            continue

        tabular_data.append(
            _generate_result_line(result, min_date, max_date, starting_balance, pair)
        )

    tabular_data = sorted(tabular_data, key=lambda k: k["profit_total_abs"], reverse=True)

    tabular_data.append(
        _generate_result_line(results, min_date, max_date, starting_balance, "TOTAL")
    )

    return tabular_data


def generate_tag_metrics(
    tag_type: Literal["enter_tag", "exit_reason"] | list[Literal["enter_tag", "exit_reason"]],
    starting_balance: float,
    results: pl.DataFrame,
    min_date: datetime,
    max_date: datetime,
    skip_nan: bool = False,
) -> list[dict]:
    tabular_data = []

    tag_cols = tag_type if isinstance(tag_type, list) else [tag_type]
    if all(tag in results.columns for tag in tag_cols):
        for group_key, group in results.group_by(tag_cols, maintain_order=True):
            if skip_nan and group["profit_abs"].is_null().all():
                continue
            # group_key is a tuple; for single tag, use scalar
            tags = group_key[0] if len(tag_cols) == 1 else list(group_key)
            tabular_data.append(
                _generate_result_line(group, min_date, max_date, starting_balance, tags)
            )

        tabular_data = sorted(tabular_data, key=lambda k: k["profit_total_abs"], reverse=True)

        tabular_data.append(
            _generate_result_line(results, min_date, max_date, starting_balance, "TOTAL")
        )
        return tabular_data
    else:
        return []


def generate_strategy_comparison(bt_stats: dict) -> list[dict]:
    tabular_data = []
    for strategy, result in bt_stats.items():
        tabular_data.append(deepcopy(result["results_per_pair"][-1]))
        tabular_data[-1]["key"] = strategy
        tabular_data[-1]["max_drawdown_account"] = result["max_drawdown_account"]
        tabular_data[-1]["max_drawdown_abs"] = fmt_coin(
            result["max_drawdown_abs"], result["stake_currency"], False
        )
    return tabular_data


def _get_resample_from_period(period: str) -> str:
    if period == "day":
        return "1d"
    if period == "week":
        return "1w"
    if period == "month":
        return "1mo"
    if period == "year":
        return "1y"
    if period == "weekday":
        return "weekday"
    raise ValueError(f"Period {period} is not supported.")


def _calculate_stats_for_period(data: pl.DataFrame) -> dict[str, Any]:
    profit_abs = round(data["profit_abs"].sum(), 10)
    wins = data.filter(pl.col("profit_abs") > 0).height
    draws = data.filter(pl.col("profit_abs") == 0).height
    losses = data.filter(pl.col("profit_abs") < 0).height
    trades = wins + draws + losses
    winning_profit = data.filter(pl.col("profit_abs") > 0)["profit_abs"].sum()
    losing_profit = data.filter(pl.col("profit_abs") < 0)["profit_abs"].sum()
    profit_factor = winning_profit / abs(losing_profit) if losing_profit else 0.0

    return {
        "profit_abs": profit_abs,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "trades": trades,
        "profit_factor": round(profit_factor, 8),
    }


def generate_periodic_breakdown_stats(
    trade_list: list | pl.DataFrame, period: str
) -> list[dict[str, Any]]:
    results = trade_list if isinstance(trade_list, pl.DataFrame) else pl.DataFrame(trade_list)
    if len(results) == 0:
        return []

    # Ensure close_date is datetime
    if results["close_date"].dtype == pl.Utf8:
        results = results.with_columns(
            pl.col("close_date").str.to_datetime(time_zone="UTC")
        )
    elif results["close_date"].dtype == pl.Int64:
        results = results.with_columns(
            pl.from_epoch(pl.col("close_date"), time_unit="ms").dt.replace_time_zone("UTC")
        )

    if period == "weekday":
        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        results = results.with_columns(
            pl.col("close_date").dt.weekday().alias("weekday")
        )

        stats = []
        for day_num in range(7):
            # polars weekday: Monday=1..Sunday=7
            day_data = results.filter(pl.col("weekday") == day_num + 1)
            if len(day_data) > 0:
                period_stats = _calculate_stats_for_period(day_data)
                stats.append({"date": day_names[day_num], "date_ts": day_num, **period_stats})
    else:
        resample_period = _get_resample_from_period(period)

        results = results.sort("close_date")
        grouped = results.group_by_dynamic("close_date", every=resample_period)
        stats = []
        for group_key, period_data in grouped:
            period_stats = _calculate_stats_for_period(period_data)
            dt = group_key[0]
            stats.append(
                {
                    "date": dt.strftime("%d/%m/%Y"),
                    "date_ts": int(dt.timestamp() * 1000),
                    **period_stats,
                }
            )

    return stats


def generate_all_periodic_breakdown_stats(trade_list: list | pl.DataFrame) -> dict[str, list]:
    result = {}
    for period in BACKTEST_BREAKDOWNS:
        result[period] = generate_periodic_breakdown_stats(trade_list, period)
    return result


def calc_streak(dataframe: pl.DataFrame) -> tuple[int, int]:
    """
    Calculate consecutive win and loss streaks
    """
    results = dataframe.select(
        pl.when(pl.col("profit_ratio") > 0)
        .then(pl.lit("win"))
        .otherwise(pl.lit("loss"))
        .alias("result")
    )

    result_col = results["result"]
    # Build streak groups: increment when result changes from previous
    shifted = result_col.shift(1)
    group_ids = (result_col != shifted).cum_sum()

    streak_df = pl.DataFrame({
        "result": result_col,
        "group": group_ids,
    })

    # Count consecutive within each group, then get max per result type
    streak_counts = (
        streak_df.group_by("group", "result")
        .agg(pl.len().alias("count"))
    )

    wins_max = streak_counts.filter(pl.col("result") == "win")
    losses_max = streak_counts.filter(pl.col("result") == "loss")

    cons_wins = int(wins_max["count"].max()) if wins_max.height > 0 else 0
    cons_losses = int(losses_max["count"].max()) if losses_max.height > 0 else 0

    return cons_wins, cons_losses


def generate_trading_stats(results: pl.DataFrame) -> dict[str, Any]:
    if len(results) == 0:
        return {
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "winrate": 0,
            "holding_avg": timedelta(),
            "winner_holding_avg": timedelta(),
            "loser_holding_avg": timedelta(),
            "max_consecutive_wins": 0,
            "max_consecutive_losses": 0,
        }

    winning_trades = results.filter(pl.col("profit_ratio") > 0)
    draw_trades = results.filter(pl.col("profit_ratio") == 0)
    losing_trades = results.filter(pl.col("profit_ratio") < 0)

    holding_avg = (
        timedelta(minutes=round(results["trade_duration"].mean()))
        if not results.is_empty()
        else timedelta()
    )
    winner_holding_min = (
        timedelta(minutes=round(winning_trades["trade_duration"].min()))
        if not winning_trades.is_empty()
        else timedelta()
    )
    winner_holding_max = (
        timedelta(minutes=round(winning_trades["trade_duration"].max()))
        if not winning_trades.is_empty()
        else timedelta()
    )
    winner_holding_avg = (
        timedelta(minutes=round(winning_trades["trade_duration"].mean()))
        if not winning_trades.is_empty()
        else timedelta()
    )
    loser_holding_min = (
        timedelta(minutes=round(losing_trades["trade_duration"].min()))
        if not losing_trades.is_empty()
        else timedelta()
    )
    loser_holding_max = (
        timedelta(minutes=round(losing_trades["trade_duration"].max()))
        if not losing_trades.is_empty()
        else timedelta()
    )
    loser_holding_avg = (
        timedelta(minutes=round(losing_trades["trade_duration"].mean()))
        if not losing_trades.is_empty()
        else timedelta()
    )
    winstreak, loss_streak = calc_streak(results)

    return {
        "wins": winning_trades.height,
        "losses": losing_trades.height,
        "draws": draw_trades.height,
        "winrate": winning_trades.height / results.height if results.height else 0.0,
        "holding_avg": holding_avg,
        "holding_avg_s": holding_avg.total_seconds(),
        "winner_holding_min": format_duration(winner_holding_min),
        "winner_holding_min_s": winner_holding_min.total_seconds(),
        "winner_holding_max": format_duration(winner_holding_max),
        "winner_holding_max_s": winner_holding_max.total_seconds(),
        "winner_holding_avg": format_duration(winner_holding_avg),
        "winner_holding_avg_s": winner_holding_avg.total_seconds(),
        "loser_holding_min": format_duration(loser_holding_min),
        "loser_holding_min_s": loser_holding_min.total_seconds(),
        "loser_holding_max": format_duration(loser_holding_max),
        "loser_holding_max_s": loser_holding_max.total_seconds(),
        "loser_holding_avg": format_duration(loser_holding_avg),
        "loser_holding_avg_s": loser_holding_avg.total_seconds(),
        "max_consecutive_wins": winstreak,
        "max_consecutive_losses": loss_streak,
    }


def generate_daily_stats(results: pl.DataFrame) -> dict[str, Any]:
    if len(results) == 0:
        return {
            "backtest_best_day": 0,
            "backtest_worst_day": 0,
            "backtest_best_day_abs": 0,
            "backtest_worst_day_abs": 0,
            "winning_days": 0,
            "draw_days": 0,
            "losing_days": 0,
            "daily_profit_list": [],
        }
    daily = (
        results.sort("close_date")
        .group_by_dynamic("close_date", every="1d")
        .agg(
            pl.col("profit_ratio").sum().alias("profit_ratio_sum"),
            pl.col("profit_abs").sum().round(10).alias("profit_abs_sum"),
        )
    )
    worst_rel = daily["profit_ratio_sum"].min()
    best_rel = daily["profit_ratio_sum"].max()
    worst = daily["profit_abs_sum"].min()
    best = daily["profit_abs_sum"].max()
    winning_days = daily.filter(pl.col("profit_abs_sum") > 0).height
    draw_days = daily.filter(pl.col("profit_abs_sum") == 0).height
    losing_days = daily.filter(pl.col("profit_abs_sum") < 0).height
    daily_profit_list = [
        (str(row[0].date()), row[1])
        for row in daily.select("close_date", "profit_abs_sum").iter_rows()
    ]

    return {
        "backtest_best_day": best_rel,
        "backtest_worst_day": worst_rel,
        "backtest_best_day_abs": best,
        "backtest_worst_day_abs": worst,
        "winning_days": winning_days,
        "draw_days": draw_days,
        "losing_days": losing_days,
        "daily_profit": daily_profit_list,
    }


def generate_strategy_stats(
    pairlist: list[str],
    strategy: str,
    content: BacktestContentType,
    min_date: datetime,
    max_date: datetime,
    market_change: float,
    is_hyperopt: bool = False,
) -> dict[str, Any]:
    results: pl.DataFrame = content["results"]
    if not isinstance(results, pl.DataFrame):
        return {}
    config = content["config"]
    max_open_trades = min(config["max_open_trades"], len(pairlist))
    start_balance = get_dry_run_wallet(config)
    stake_currency = config["stake_currency"]

    pair_results = generate_pair_metrics(
        pairlist,
        stake_currency=stake_currency,
        starting_balance=start_balance,
        results=results,
        min_date=min_date,
        max_date=max_date,
        skip_nan=False,
    )

    enter_tag_stats = generate_tag_metrics(
        "enter_tag",
        starting_balance=start_balance,
        results=results,
        min_date=min_date,
        max_date=max_date,
        skip_nan=False,
    )
    exit_reason_stats = generate_tag_metrics(
        "exit_reason",
        starting_balance=start_balance,
        results=results,
        min_date=min_date,
        max_date=max_date,
        skip_nan=False,
    )
    mix_tag_stats = generate_tag_metrics(
        ["enter_tag", "exit_reason"],
        starting_balance=start_balance,
        results=results,
        min_date=min_date,
        max_date=max_date,
        skip_nan=False,
    )
    left_open_results = generate_pair_metrics(
        pairlist,
        stake_currency=stake_currency,
        starting_balance=start_balance,
        results=results.filter(pl.col("exit_reason") == "force_exit"),
        min_date=min_date,
        max_date=max_date,
        skip_nan=True,
    )

    daily_stats = generate_daily_stats(results)
    trade_stats = generate_trading_stats(results)

    periodic_breakdown = {}
    if not is_hyperopt:
        periodic_breakdown = {"periodic_breakdown": generate_all_periodic_breakdown_stats(results)}

    best_pair = (
        max(
            [pair for pair in pair_results if pair["key"] != "TOTAL"],
            key=lambda x: x["profit_total_abs"],
        )
        if len(pair_results) > 1
        else None
    )
    worst_pair = (
        min(
            [pair for pair in pair_results if pair["key"] != "TOTAL"],
            key=lambda x: x["profit_total_abs"],
        )
        if len(pair_results) > 1
        else None
    )
    winning_profit = results.filter(pl.col("profit_abs") > 0)["profit_abs"].sum()
    losing_profit = results.filter(pl.col("profit_abs") < 0)["profit_abs"].sum()
    profit_factor = winning_profit / abs(losing_profit) if losing_profit else 0.0

    n = results.height
    expectancy, expectancy_ratio = calculate_expectancy(results)
    backtest_days = (max_date - min_date).days or 1
    trades_dict = results.to_dicts()
    strat_stats = {
        "trades": trades_dict,
        "locks": [lock.to_json() for lock in content["locks"]],
        "best_pair": best_pair,
        "worst_pair": worst_pair,
        "results_per_pair": pair_results,
        "results_per_enter_tag": enter_tag_stats,
        "exit_reason_summary": exit_reason_stats,
        "mix_tag_stats": mix_tag_stats,
        "left_open_trades": left_open_results,
        "total_trades": n,
        "trade_count_long": results.filter(~pl.col("is_short")).height,
        "trade_count_short": results.filter(pl.col("is_short")).height,
        "total_volume": calculate_trade_volume(trades_dict),
        "avg_stake_amount": results["stake_amount"].mean() if n > 0 else 0,
        "profit_mean": results["profit_ratio"].mean() if n > 0 else 0,
        "profit_median": results["profit_ratio"].median() if n > 0 else 0,
        "profit_total": results["profit_abs"].sum() / start_balance,
        "profit_total_long": results.filter(~pl.col("is_short"))["profit_abs"].sum() / start_balance,
        "profit_total_short": results.filter(pl.col("is_short"))["profit_abs"].sum() / start_balance,
        "profit_total_abs": results["profit_abs"].sum(),
        "profit_total_long_abs": results.filter(~pl.col("is_short"))["profit_abs"].sum(),
        "profit_total_short_abs": results.filter(pl.col("is_short"))["profit_abs"].sum(),
        "cagr": calculate_cagr(backtest_days, start_balance, content["final_balance"]),
        "expectancy": expectancy,
        "expectancy_ratio": expectancy_ratio,
        "sortino": calculate_sortino(results, min_date, max_date, start_balance),
        "sharpe": calculate_sharpe(results, min_date, max_date, start_balance),
        "calmar": calculate_calmar(results, min_date, max_date, start_balance),
        "sqn": calculate_sqn(results, start_balance),
        "profit_factor": profit_factor,
        "backtest_start": min_date.strftime(DATETIME_PRINT_FORMAT),
        "backtest_start_ts": int(min_date.timestamp() * 1000),
        "backtest_end": max_date.strftime(DATETIME_PRINT_FORMAT),
        "backtest_end_ts": int(max_date.timestamp() * 1000),
        "backtest_days": backtest_days,
        "backtest_run_start_ts": content["backtest_start_time"],
        "backtest_run_end_ts": content["backtest_end_time"],
        "trades_per_day": round(n / backtest_days, 2),
        "market_change": market_change,
        "pairlist": pairlist,
        "stake_amount": config["stake_amount"],
        "stake_currency": config["stake_currency"],
        "stake_currency_decimals": decimals_per_coin(config["stake_currency"]),
        "starting_balance": start_balance,
        "dry_run_wallet": start_balance,
        "final_balance": content["final_balance"],
        "rejected_signals": content["rejected_signals"],
        "timedout_entry_orders": content["timedout_entry_orders"],
        "timedout_exit_orders": content["timedout_exit_orders"],
        "canceled_trade_entries": content["canceled_trade_entries"],
        "canceled_entry_orders": content["canceled_entry_orders"],
        "replaced_entry_orders": content["replaced_entry_orders"],
        "max_open_trades": max_open_trades,
        "max_open_trades_setting": (
            config["max_open_trades"] if config["max_open_trades"] != float("inf") else -1
        ),
        "timeframe": config["timeframe"],
        "timeframe_detail": config.get("timeframe_detail", ""),
        "timerange": config.get("timerange", ""),
        "enable_protections": config.get("enable_protections", False),
        "strategy_name": strategy,
        # Parameters relevant for backtesting
        "stoploss": config["stoploss"],
        "trailing_stop": config.get("trailing_stop", False),
        "trailing_stop_positive": config.get("trailing_stop_positive"),
        "trailing_stop_positive_offset": config.get("trailing_stop_positive_offset", 0.0),
        "trailing_only_offset_is_reached": config.get("trailing_only_offset_is_reached", False),
        "use_custom_stoploss": config.get("use_custom_stoploss", False),
        "minimal_roi": config["minimal_roi"],
        "use_exit_signal": config["use_exit_signal"],
        "exit_profit_only": config["exit_profit_only"],
        "exit_profit_offset": config["exit_profit_offset"],
        "ignore_roi_if_entry_signal": config["ignore_roi_if_entry_signal"],
        "trading_mode": config["trading_mode"],
        "margin_mode": config["margin_mode"],
        **periodic_breakdown,
        **daily_stats,
        **trade_stats,
    }

    try:
        drawdown = calculate_max_drawdown(
            results, value_col="profit_abs", starting_balance=start_balance
        )
        underwater = calculate_max_drawdown(
            results, value_col="profit_abs", starting_balance=start_balance, relative=True
        )
        drawdown_duration = drawdown.low_date - drawdown.high_date

        strat_stats.update(
            {
                "max_drawdown_account": drawdown.relative_account_drawdown,
                "max_relative_drawdown": underwater.relative_account_drawdown,
                "max_drawdown_abs": drawdown.drawdown_abs,
                "drawdown_start": drawdown.high_date.strftime(DATETIME_PRINT_FORMAT),
                "drawdown_start_ts": drawdown.high_date.timestamp() * 1000,
                "drawdown_end": drawdown.low_date.strftime(DATETIME_PRINT_FORMAT),
                "drawdown_end_ts": drawdown.low_date.timestamp() * 1000,
                "drawdown_duration": drawdown_duration,
                "drawdown_duration_s": drawdown_duration.total_seconds(),
                "max_drawdown_low": drawdown.low_value,
                "max_drawdown_high": drawdown.high_value,
            }
        )

        csum_min, csum_max = calculate_csum(results, start_balance)
        strat_stats.update({"csum_min": csum_min, "csum_max": csum_max})

    except ValueError:
        strat_stats.update(
            {
                "max_drawdown_account": 0.0,
                "max_relative_drawdown": 0.0,
                "max_drawdown_abs": 0.0,
                "max_drawdown_low": 0.0,
                "max_drawdown_high": 0.0,
                "drawdown_start": datetime(1970, 1, 1, tzinfo=UTC),
                "drawdown_start_ts": 0,
                "drawdown_end": datetime(1970, 1, 1, tzinfo=UTC),
                "drawdown_end_ts": 0,
                "csum_min": 0,
                "csum_max": 0,
            }
        )

    return strat_stats


def generate_backtest_stats(
    btdata: dict[str, pl.DataFrame],
    all_results: dict[str, BacktestContentType],
    min_date: datetime,
    max_date: datetime,
    notes: str | None = None,
) -> BacktestResultType:
    result: BacktestResultType = get_BacktestResultType_default()
    market_change = calculate_market_change(btdata, "close", min_date=min_date)
    metadata = {}
    pairlist = list(btdata.keys())
    for strategy, content in all_results.items():
        strat_stats = generate_strategy_stats(
            pairlist, strategy, content, min_date, max_date, market_change=market_change
        )
        metadata[strategy] = {
            "run_id": content["run_id"],
            "backtest_start_time": content["backtest_start_time"],
            "timeframe": content["config"]["timeframe"],
            "timeframe_detail": content["config"].get("timeframe_detail", None),
            "backtest_start_ts": int(min_date.timestamp()),
            "backtest_end_ts": int(max_date.timestamp()),
        }
        if notes:
            metadata[strategy]["notes"] = notes
        result["strategy"][strategy] = strat_stats

    strategy_results = generate_strategy_comparison(bt_stats=result["strategy"])

    result["metadata"] = metadata
    result["strategy_comparison"] = strategy_results

    return result
