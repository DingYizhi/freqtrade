"""Handle local historic data used by backtesting."""

# flake8: noqa: F401
from .datahandlers import get_datahandler
from .history_utils import (
    get_timerange,
    load_data,
    load_pair_history,
    validate_backtest_data,
)
