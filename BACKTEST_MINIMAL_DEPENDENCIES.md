# Backtesting Minimal Dependency Checklist

This fork is intentionally reduced to a backtesting-first codebase.

## Runtime-critical paths

These paths are part of the current supported backtest flow and should stay stable unless the entrypoint is redesigned.

- `freqtrade/`
- `scripts/`
- `requirements.txt`
- `requirements-dev.txt`
- `pyproject.toml`
- `README.md`

## Test-only or historical paths

These paths are not required by the supported runtime entrypoint `scripts/mock_backtest.py`.

- `tests/`
  - `tests/testdata/` is only used by the legacy test suite.
- `user_data/`
  - The supported workflow uses the external directory `D:/_code/ft_userdata/user_data`.
  - The repository-local `user_data/` tree is not used by the default mock backtest flow.

## Confirmed current runtime boundary

The supported backtest path is:

`scripts/mock_backtest.py` -> `Configuration` -> `Backtesting`

Default injected paths point to the external workspace:

- `D:/_code/ft_userdata/user_data/config.json`
- `D:/_code/ft_userdata/user_data/strategies`
- `D:/_code/ft_userdata/user_data/backtest_results`

Supported historical data storage format for this fork is `feather` only.

## Safe pruning rule

If a directory is not imported by the above runtime path and is only used by upstream tests, packaging, docs, RPC, live trading, or sample assets, it is outside the minimal backtesting runtime surface and can be removed from this fork.

## Applied by this cleanup pass

The following directories are intentionally removed by this pass:

- `tests/testdata/`
- `user_data/`

## Python modules explicitly removed from runtime surface

These Python-only features are now outside the supported backtesting fork and have been removed:

- Hyperopt result tooling, loss-function loaders, and bundled hyperopt loss modules.
- RPC message enums that only served webhook / telegram / API message flows.
- Entry / exit analysis helpers that were only reachable from removed CLI commands.
- JSON, gzipped JSON, and parquet historical data handlers.
- Exchange websocket support and its runtime/configuration hooks.
- Strategy plotting annotation types and template-rendering helpers.
- Legacy system helpers and legacy database auto-migration support.
- The legacy freqtrade.commands package and qtpylib vendor compatibility layer.

Strategy parameter JSON loading is still supported for backtest runs, but it no longer depends on the Hyperopt runtime toolchain.