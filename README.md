# Freqtrade Backtesting Fork

This repository is a heavily reduced fork focused on local backtesting only.

Current scope:

- Backtesting-first execution through [scripts/mock_backtest.py](scripts/mock_backtest.py)
- Binance-focused exchange support
- Minimal CLI and configuration surface needed for backtest workflows
- No docs site, no Docker packaging, no RPC/API server, no FreqAI, no plotting stack

The stable runtime boundary for this fork is documented in [BACKTEST_MINIMAL_DEPENDENCIES.md](BACKTEST_MINIMAL_DEPENDENCIES.md).

## Usage

Run the supported entrypoint directly:

```powershell
python scripts/mock_backtest.py --strategy Mar3Strategy --timeframe 1m --timerange 20220301-20220401 --cache none
```

The script injects the minimum required configuration defaults for local mock backtests and keeps the exchange bootstrap offline.

## Install

Base install:

```powershell
pip install -r requirements.txt
pip install -e .
```

Development install:

```powershell
pip install -r requirements-dev.txt
pip install -e .
```

## Validation

Typical local checks:

```powershell
python scripts/mock_backtest.py --strategy Mar3Strategy --timeframe 1m --timerange 20220301-20220401 --cache none
```

## Basic Usage

### Backtest entry

This fork is now API-first for backtesting work. The default entrypoint is [scripts/mock_backtest.py](scripts/mock_backtest.py), which drives [freqtrade/optimize/backtesting.py](freqtrade/optimize/backtesting.py) directly instead of going through the old package CLI.

```powershell
python scripts/mock_backtest.py --strategy Mar3Strategy --timeframe 1m --timerange 20220301-20220401 --cache none
```

The script injects these defaults when they are not provided explicitly:

- `--config D:/_code/ft_userdata/user_data/config.json`
- `--user-data-dir D:/_code/ft_userdata/user_data`
- `--strategy-path D:/_code/ft_userdata/user_data/strategies`

## Requirements

### Up-to-date clock

The clock must be accurate, synchronized to a NTP server very frequently to avoid problems with communication to the exchanges.

### Minimum hardware required

To run this bot we recommend you a cloud instance with a minimum of:

- Minimal (advised) system requirements: 2GB RAM, 1GB disk space, 2vCPU

### Software requirements

- [Python >= 3.11](http://docs.python-guide.org/en/latest/starting/installation/)
- [pip](https://pip.pypa.io/en/stable/installing/)
- [git](https://git-scm.com/book/en/v2/Getting-Started-Installing-Git)
- [TA-Lib](https://ta-lib.github.io/ta-lib-python/)
- [virtualenv](https://virtualenv.pypa.io/en/stable/installation.html) (Recommended)
