import logging
from datetime import datetime

import polars as pl

from freqtrade.exceptions import StrategyError


logger = logging.getLogger(__name__)


class StrategyResultValidator:
    def __init__(self, dataframe: pl.DataFrame, warn_only: bool = False):
        self._warn_only = warn_only
        self._length: int = len(dataframe)
        self._close: float = dataframe[-1, "close"]
        self._date: datetime = dataframe[-1, "date"]

    def assert_df(self, dataframe: pl.DataFrame):
        """
        Ensure dataframe (length, last candle) was not modified, and has all elements we need.
        """
        message_template = "Dataframe returned from strategy has mismatching {}."
        message = ""
        if dataframe is None:
            message = "No dataframe returned (return statement missing?)."
        elif self._length != len(dataframe):
            message = message_template.format("length")
        elif self._close != dataframe[-1, "close"]:
            message = message_template.format("last close price")
        elif self._date != dataframe[-1, "date"]:
            message = message_template.format("last date")
        if message:
            if self._warn_only:
                logger.warning(message)
            else:
                raise StrategyError(message)
