import numpy as np
import polars as pl


class TickSizeLookup:
    """Pre-computed tick-size lookup using numpy binary search (replaces pandas Series.asof)."""

    __slots__ = ("_ts_ns", "_values")

    def __init__(self, ts_ns: np.ndarray, values: np.ndarray):
        self._ts_ns = ts_ns  # int64 nanosecond timestamps
        self._values = values

    def asof(self, dt) -> float:
        ts = int(dt.timestamp() * 1_000_000_000)
        idx = np.searchsorted(self._ts_ns, ts, side="right") - 1
        if idx < 0:
            return float("nan")
        return self._values[idx]


def get_tick_size_over_time(candles) -> TickSizeLookup:
    """
    Calculate the number of significant digits for candles over time.
    Uses the monthly maximum of the number of significant digits.
    Returns a TickSizeLookup for fast asof-style queries.
    """
    cols = ["date", "open", "high", "low", "close"]
    if isinstance(candles, pl.DataFrame):
        df = candles.select(cols)
    else:
        df = pl.from_pandas(candles[cols])

    # Vectorized: cast float to string, extract significant decimal digits count
    count_exprs = []
    for col in ["open", "high", "low", "close"]:
        count_exprs.append(
            pl.col(col).cast(pl.Utf8)
            .str.extract(r"\.(\d*[1-9])", 1)
            .str.len_chars()
            .alias(f"{col}_count")
        )

    df = df.with_columns(count_exprs)
    df = df.with_columns(
        pl.max_horizontal("open_count", "high_count", "low_count", "close_count")
        .alias("max_count")
    )

    # Group by month start, take max significant digits
    monthly = (
        df.group_by_dynamic("date", every="1mo")
        .agg(pl.col("max_count").max())
    )

    # Convert digit count to tick size: 2 -> 0.01, 5 -> 0.00001
    monthly = monthly.with_columns(
        (1.0 / (10.0 ** pl.col("max_count"))).alias("tick_size")
    )

    ts_ns = monthly["date"].to_numpy().astype("datetime64[ns]").astype(np.int64)
    values = monthly["tick_size"].to_numpy().astype(np.float64)
    return TickSizeLookup(ts_ns, values)
