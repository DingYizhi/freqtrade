import pytest

from freqtrade.data.dataprovider import DataProvider
from freqtrade.enums import RunMode
from freqtrade.exceptions import OperationalException
from tests.conftest import EXMS, get_patched_exchange


def test_refresh(mocker, default_conf):
    refresh_mock = mocker.patch(f"{EXMS}.refresh_latest_ohlcv")
    mock_refresh_trades = mocker.patch(f"{EXMS}.refresh_latest_trades")

    exchange = get_patched_exchange(mocker, default_conf, exchange="binance")
    timeframe = default_conf["timeframe"]
    pairs = [("XRP/BTC", timeframe), ("UNITTEST/BTC", timeframe)]

    dp = DataProvider(default_conf, exchange)
    dp.refresh(pairs)
    assert mock_refresh_trades.call_count == 0
    assert refresh_mock.call_count == 1
    assert refresh_mock.call_args[0][0] == pairs

    with pytest.raises(OperationalException):
        dp = DataProvider(default_conf, exchange)
        dp.current_whitelist()


def test_dp_send_msg(default_conf):
    default_conf["runmode"] = RunMode.DRY_RUN
    default_conf["timeframe"] = "1h"
    dp = DataProvider(default_conf, None)
    msg = "Test message"
    dp.send_msg(msg)

    assert msg in dp._msg_queue
    dp._msg_queue.pop()
    assert msg not in dp._msg_queue

    dp.send_msg(msg)
    assert msg not in dp._msg_queue
    dp.send_msg(msg, always_send=True)
    assert msg in dp._msg_queue

    default_conf["runmode"] = RunMode.BACKTEST
    dp = DataProvider(default_conf, None)
    dp.send_msg(msg, always_send=True)
    assert msg not in dp._msg_queue


def test_dp_get_required_startup(default_conf_usdt):
    default_conf_usdt["timeframe"] = "1h"
    dp = DataProvider(default_conf_usdt, None)

    assert dp.get_required_startup("5m") == 0
    assert dp.get_required_startup("1h") == 0
    assert dp.get_required_startup("1d") == 0

    dp._config["startup_candle_count"] = 20
    assert dp.get_required_startup("5m") == 20
    assert dp.get_required_startup("1h") == 20
    assert dp.get_required_startup("1d") == 20
