from pathlib import Path

from freqtrade.configuration import setup_utils_configuration
from freqtrade.enums import RunMode
from tests.conftest import (
    CURRENT_TEST_STRATEGY,
    get_args,
    log_has,
    log_has_re,
    patched_configuration_load_config_file,
)


def test_setup_optimize_configuration_without_arguments(mocker, default_conf, caplog) -> None:
    patched_configuration_load_config_file(mocker, default_conf)

    args = [
        "backtesting",
        "--config",
        "config.json",
        "--strategy",
        CURRENT_TEST_STRATEGY,
        "--export",
        "none",
    ]

    config = setup_utils_configuration(get_args(args), RunMode.BACKTEST)
    assert "max_open_trades" in config
    assert "stake_currency" in config
    assert "stake_amount" in config
    assert "exchange" in config
    assert "pair_whitelist" in config["exchange"]
    assert "datadir" in config
    assert log_has(f"Using data directory: {config['datadir']} ...", caplog)
    assert "timeframe" in config
    assert not log_has_re("Parameter -i/--ticker-interval detected .*", caplog)
    assert "position_stacking" not in config
    assert not log_has("Parameter --enable-position-stacking detected ...", caplog)
    assert "timerange" not in config
    assert config["export"] == "none"
    assert config["runmode"] == RunMode.BACKTEST


def test_setup_bt_configuration_with_arguments(mocker, default_conf, caplog) -> None:
    patched_configuration_load_config_file(mocker, default_conf)
    mocker.patch("freqtrade.configuration.configuration.create_datadir", lambda c, x: x)

    args = [
        "backtesting",
        "--config",
        "config.json",
        "--strategy",
        CURRENT_TEST_STRATEGY,
        "--datadir",
        "/foo/bar",
        "--timeframe",
        "1m",
        "--enable-position-stacking",
        "--timerange",
        ":100",
        "--export-filename",
        "foo_bar.json",
        "--fee",
        "0",
    ]

    config = setup_utils_configuration(get_args(args), RunMode.BACKTEST)
    assert "max_open_trades" in config
    assert "stake_currency" in config
    assert "stake_amount" in config
    assert "exchange" in config
    assert "pair_whitelist" in config["exchange"]
    assert "datadir" in config
    assert config["runmode"] == RunMode.BACKTEST
    assert log_has(f"Using data directory: {config['datadir']} ...", caplog)
    assert log_has("Parameter -i/--timeframe detected ... Using timeframe: 1m ...", caplog)
    assert "position_stacking" in config
    assert log_has("Parameter --enable-position-stacking detected ...", caplog)
    assert log_has(f"Parameter --timerange detected: {config['timerange']} ...", caplog)
    assert isinstance(config["exportfilename"], Path)
    assert log_has(f"Storing backtest results to {config['exportfilename']} ...", caplog)
    assert log_has_re(
        "DEPRECATED: Using `--export-filename` has no impact when backtesting.*",
        caplog,
    )
    assert log_has(f"Parameter --fee detected, setting fee to: {config['fee']} ...", caplog)
