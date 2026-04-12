"""Minimal static pairlist manager for backtesting-only builds."""

import logging

from freqtrade.constants import Config, ListPairsWithTimeframes
from freqtrade.data.dataprovider import DataProvider
from freqtrade.exceptions import OperationalException
from freqtrade.mixins import LoggingMixin
from freqtrade.plugins.pairlist.pairlist_helpers import expand_pairlist


logger = logging.getLogger(__name__)


class PairListManager(LoggingMixin):
    def __init__(self, exchange, config: Config, dataprovider: DataProvider | None = None) -> None:
        self._exchange = exchange
        self._config = config
        self._whitelist = list(self._config["exchange"].get("pair_whitelist", []))
        self._blacklist = self._config["exchange"].get("pair_blacklist", [])
        self._dataprovider: DataProvider | None = dataprovider

        for pairlist_handler_config in self._config.get("pairlists", [{"method": "StaticPairList"}]):
            method = pairlist_handler_config.get("method")
            if method != "StaticPairList":
                raise OperationalException(
                    "This fork only supports StaticPairList during backtesting."
                )

        refresh_period = config.get("pairlist_refresh_period", 3600)
        LoggingMixin.__init__(self, logger, refresh_period)

    @property
    def whitelist(self) -> list[str]:
        return self._whitelist

    @property
    def blacklist(self) -> list[str]:
        return self._blacklist

    @property
    def name_list(self) -> list[str]:
        return ["StaticPairList"]

    def short_desc(self) -> list[dict]:
        return [{"StaticPairList": "Uses exchange.pair_whitelist as-is."}]

    def refresh_pairlist(self, only_first: bool = False, pairs: list[str] | None = None) -> None:
        pairlist = self.verify_whitelist(self._whitelist, logger.warning)
        if pairs is not None:
            pairlist = [pair for pair in pairlist if pair in pairs]
        pairlist = self.verify_blacklist(pairlist, logger.warning)
        self.log_once(f"Whitelist with {len(pairlist)} pairs: {pairlist}", logger.info)
        self._whitelist = pairlist

    def verify_blacklist(self, pairlist: list[str], logmethod) -> list[str]:
        if self._blacklist:
            try:
                blacklist = expand_pairlist(self._blacklist, list(self._exchange.get_markets().keys()))
            except ValueError as err:
                logger.error(f"Pair blacklist contains an invalid Wildcard: {err}")
                return []

            for pair in pairlist.copy():
                if pair in blacklist:
                    self.log_once(
                        f"Pair {pair} in your blacklist. Removing it from whitelist...",
                        logmethod,
                    )
                    pairlist.remove(pair)
        return pairlist

    def verify_whitelist(
        self, pairlist: list[str], logmethod, keep_invalid: bool = False
    ) -> list[str]:
        try:
            return expand_pairlist(
                pairlist,
                list(self._exchange.get_markets().keys()),
                keep_invalid,
            )
        except ValueError as err:
            logger.error(f"Pair whitelist contains an invalid Wildcard: {err}")
            return []

    def create_pair_list(
        self, pairs: list[str], timeframe: str | None = None, candle_type=None
    ) -> ListPairsWithTimeframes:
        return [(pair, timeframe, candle_type) for pair in pairs]
