# Required json-schema for user specified config


from freqtrade.constants import (
    AVAILABLE_DATAHANDLERS,
    AVAILABLE_PAIRLISTS,
    BACKTEST_BREAKDOWNS,
    BACKTEST_CACHE_AGE,
    DRY_RUN_WALLET,
    EXPORT_OPTIONS,
    MARGIN_MODES,
    ORDERTIF_POSSIBILITIES,
    ORDERTYPE_POSSIBILITIES,
    PRICING_SIDES,
    REQUIRED_ORDERTIF,
    STOPLOSS_PRICE_TYPES,
    SUPPORTED_FIAT,
    TIMEOUT_UNITS,
    TRADING_MODES,
    UNLIMITED_STAKE_AMOUNT,
)

__IN_STRATEGY = "\nUsually specified in the strategy and missing in the configuration."

__VIA_ENV = "Recommended to be set via environment variable"

CONF_SCHEMA = {
    "type": "object",
    "properties": {
        "max_open_trades": {
            "description": "Maximum number of open trades. -1 for unlimited.",
            "type": ["integer", "number"],
            "minimum": -1,
        },
        "timeframe": {
            "description": (
                f"The timeframe to use (e.g `1m`, `5m`, `15m`, `30m`, `1h` ...). {__IN_STRATEGY}"
            ),
            "type": "string",
        },
        "proxy_coin": {
            "description": "Proxy coin - must be used for specific futures modes (e.g. BNFCR)",
            "type": "string",
        },
        "stake_currency": {
            "description": "Currency used for staking.",
            "type": "string",
        },
        "stake_amount": {
            "description": "Amount to stake per trade.",
            "type": ["number", "string"],
            "minimum": 0.0001,
            "pattern": UNLIMITED_STAKE_AMOUNT,
        },
        "tradable_balance_ratio": {
            "description": "Ratio of balance that is tradable.",
            "type": "number",
            "minimum": 0.0,
            "maximum": 1,
            "default": 0.99,
        },
        "available_capital": {
            "description": "Total capital available for trading.",
            "type": "number",
            "minimum": 0,
        },
        "amend_last_stake_amount": {
            "description": "Whether to amend the last stake amount.",
            "type": "boolean",
            "default": False,
        },
        "last_stake_amount_min_ratio": {
            "description": "Minimum ratio for the last stake amount.",
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "default": 0.5,
        },
        "fiat_display_currency": {
            "description": "Fiat currency for display purposes.",
            "type": "string",
            "enum": SUPPORTED_FIAT,
        },
        "dry_run": {
            "description": "Enable or disable dry run mode.",
            "type": "boolean",
        },
        "dry_run_wallet": {
            "description": "Initial wallet balance for dry run mode.",
            "type": ["number", "object"],
            "default": DRY_RUN_WALLET,
            "patternProperties": {r"^[a-zA-Z0-9]+$": {"type": "number"}},
            "additionalProperties": False,
        },
        "cancel_open_orders_on_exit": {
            "description": "Cancel open orders when exiting.",
            "type": "boolean",
            "default": False,
        },
        "process_only_new_candles": {
            "description": "Process only new candles.",
            "type": "boolean",
        },
        "minimal_roi": {
            "description": f"Minimum return on investment. {__IN_STRATEGY}",
            "type": "object",
            "patternProperties": {"^[0-9.]+$": {"type": "number"}},
        },
        "amount_reserve_percent": {
            "description": "Percentage of amount to reserve.",
            "type": "number",
            "minimum": 0.0,
            "maximum": 0.5,
        },
        "stoploss": {
            "description": f"Value (as ratio) to use as Stoploss value. {__IN_STRATEGY}",
            "type": "number",
            "maximum": 0,
            "exclusiveMaximum": True,
        },
        "trailing_stop": {
            "description": f"Enable or disable trailing stop. {__IN_STRATEGY}",
            "type": "boolean",
        },
        "trailing_stop_positive": {
            "description": f"Positive offset for trailing stop. {__IN_STRATEGY}",
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "trailing_stop_positive_offset": {
            "description": f"Offset for trailing stop to activate. {__IN_STRATEGY}",
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "trailing_only_offset_is_reached": {
            "description": f"Use trailing stop only when offset is reached. {__IN_STRATEGY}",
            "type": "boolean",
        },
        "use_exit_signal": {
            "description": f"Use exit signal for trades. {__IN_STRATEGY}",
            "type": "boolean",
        },
        "exit_profit_only": {
            "description": (
                "Exit only when in profit. Exit signals are ignored as "
                f"long as profit is < exit_profit_offset. {__IN_STRATEGY}"
            ),
            "type": "boolean",
        },
        "exit_profit_offset": {
            "description": f"Offset for profit exit. {__IN_STRATEGY}",
            "type": "number",
        },
        "recursive_strategy_search": {
            "description": "Enable recursive strategy search.",
            "type": "boolean",
        },
        "user_data_dir": {
            "description": "Path to the user data directory.",
        },
        "datadir": {
            "description": "Path to the data directory.",
        },
        "fee": {
            "description": "Trading fee percentage. Can help to simulate slippage in backtesting",
            "type": "number",
            "minimum": 0,
            "maximum": 0.1,
        },
        "ignore_roi_if_entry_signal": {
            "description": f"Ignore ROI if entry signal is present. {__IN_STRATEGY}",
            "type": "boolean",
        },
        "ignore_buying_expired_candle_after": {
            "description": f"Ignore buying after candle expiration time. {__IN_STRATEGY}",
            "type": "number",
        },
        "trading_mode": {
            "description": "Mode of trading (e.g., spot, margin).",
            "type": "string",
            "enum": TRADING_MODES,
        },
        "margin_mode": {
            "description": "Margin mode for trading.",
            "type": "string",
            "enum": MARGIN_MODES,
        },
        "reduce_df_footprint": {
            "description": "Reduce DataFrame footprint by casting columns to float32/int32.",
            "type": "boolean",
            "default": False,
        },
        # Lookahead analysis section
        "minimum_trade_amount": {
            "description": "Minimum amount for a trade - only used for lookahead-analysis",
            "type": "number",
            "default": 10,
        },
        "targeted_trade_amount": {
            "description": "Targeted trade amount for lookahead analysis.",
            "type": "number",
            "default": 20,
        },
        "lookahead_analysis_exportfilename": {
            "description": "csv Filename for lookahead analysis export.",
            "type": "string",
        },
        "startup_candle": {
            "description": "Startup candle configuration.",
            "type": "array",
            "uniqueItems": True,
            "default": [199, 399, 499, 999, 1999],
        },
        "liquidation_buffer": {
            "description": "Buffer ratio for liquidation.",
            "type": "number",
            "minimum": 0.0,
            "maximum": 0.99,
        },
        "backtest_breakdown": {
            "description": "Breakdown configuration for backtesting.",
            "type": "array",
            "items": {"type": "string", "enum": BACKTEST_BREAKDOWNS},
        },
        "backtest_cache": {
            "description": "Load a cached backtest result no older than specified age.",
            "type": "string",
            "enum": BACKTEST_CACHE_AGE,
        },
        "bot_name": {
            "description": "Name of the trading bot. Passed via API to a client.",
            "type": "string",
        },
        "unfilledtimeout": {
            "description": f"Timeout configuration for unfilled orders. {__IN_STRATEGY}",
            "type": "object",
            "properties": {
                "entry": {
                    "description": "Timeout for entry orders in unit.",
                    "type": "number",
                    "minimum": 1,
                },
                "exit": {
                    "description": "Timeout for exit orders in unit.",
                    "type": "number",
                    "minimum": 1,
                },
                "exit_timeout_count": {
                    "description": "Number of times to retry exit orders before giving up.",
                    "type": "number",
                    "minimum": 0,
                    "default": 0,
                },
                "unit": {
                    "description": "Unit of time for the timeout (e.g., seconds, minutes).",
                    "type": "string",
                    "enum": TIMEOUT_UNITS,
                    "default": "minutes",
                },
            },
        },
        "entry_pricing": {
            "description": "Configuration for entry pricing.",
            "type": "object",
            "properties": {
                "price_last_balance": {
                    "description": "Balance ratio for the last price.",
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "exclusiveMaximum": False,
                },
                "price_side": {
                    "description": "Side of the price to use (e.g., bid, ask, same).",
                    "type": "string",
                    "enum": PRICING_SIDES,
                    "default": "same",
                },
                "use_order_book": {
                    "description": "Whether to use the order book for pricing.",
                    "type": "boolean",
                },
                "order_book_top": {
                    "description": "Top N levels of the order book to consider.",
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                },
                "check_depth_of_market": {
                    "description": "Configuration for checking the depth of the market.",
                    "type": "object",
                    "properties": {
                        "enabled": {
                            "description": "Enable or disable depth of market check.",
                            "type": "boolean",
                        },
                        "bids_to_ask_delta": {
                            "description": "Delta between bids and asks to consider.",
                            "type": "number",
                            "minimum": 0,
                        },
                    },
                },
            },
            "required": ["price_side"],
        },
        "exit_pricing": {
            "description": "Configuration for exit pricing.",
            "type": "object",
            "properties": {
                "price_side": {
                    "description": "Side of the price to use (e.g., bid, ask, same).",
                    "type": "string",
                    "enum": PRICING_SIDES,
                    "default": "same",
                },
                "price_last_balance": {
                    "description": "Balance ratio for the last price.",
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "exclusiveMaximum": False,
                },
                "use_order_book": {
                    "description": "Whether to use the order book for pricing.",
                    "type": "boolean",
                },
                "order_book_top": {
                    "description": "Top N levels of the order book to consider.",
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "required": ["price_side"],
        },
        "custom_price_max_distance_ratio": {
            "description": "Maximum distance ratio between current and custom entry or exit price.",
            "type": "number",
            "minimum": 0.0,
            "maximum": 1,
            "default": 0.02,
        },
        "order_types": {
            "description": f"Configuration of order types. {__IN_STRATEGY}",
            "type": "object",
            "properties": {
                "entry": {
                    "description": "Order type for entry (e.g., limit, market).",
                    "type": "string",
                    "enum": ORDERTYPE_POSSIBILITIES,
                },
                "exit": {
                    "description": "Order type for exit (e.g., limit, market).",
                    "type": "string",
                    "enum": ORDERTYPE_POSSIBILITIES,
                },
                "force_exit": {
                    "description": "Order type for forced exit (e.g., limit, market).",
                    "type": "string",
                    "enum": ORDERTYPE_POSSIBILITIES,
                },
                "force_entry": {
                    "description": "Order type for forced entry (e.g., limit, market).",
                    "type": "string",
                    "enum": ORDERTYPE_POSSIBILITIES,
                },
                "emergency_exit": {
                    "description": "Order type for emergency exit (e.g., limit, market).",
                    "type": "string",
                    "enum": ORDERTYPE_POSSIBILITIES,
                    "default": "market",
                },
                "stoploss": {
                    "description": "Order type for stop loss (e.g., limit, market).",
                    "type": "string",
                    "enum": ORDERTYPE_POSSIBILITIES,
                },
                "stoploss_on_exchange": {
                    "description": "Whether to place stop loss on the exchange.",
                    "type": "boolean",
                },
                "stoploss_price_type": {
                    "description": "Price type for stop loss (e.g., last, mark, index).",
                    "type": "string",
                    "enum": STOPLOSS_PRICE_TYPES,
                },
                "stoploss_on_exchange_interval": {
                    "description": "Interval for stop loss on exchange in seconds.",
                    "type": "number",
                },
                "stoploss_on_exchange_limit_ratio": {
                    "description": "Limit ratio for stop loss on exchange.",
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
            },
            "required": ["entry", "exit", "stoploss", "stoploss_on_exchange"],
        },
        "order_time_in_force": {
            "description": f"Time in force configuration for orders. {__IN_STRATEGY}",
            "type": "object",
            "properties": {
                "entry": {
                    "description": "Time in force for entry orders.",
                    "type": "string",
                    "enum": ORDERTIF_POSSIBILITIES,
                },
                "exit": {
                    "description": "Time in force for exit orders.",
                    "type": "string",
                    "enum": ORDERTIF_POSSIBILITIES,
                },
            },
            "required": REQUIRED_ORDERTIF,
        },
        "coingecko": {
            "description": "Configuration for CoinGecko API.",
            "type": "object",
            "properties": {
                "is_demo": {
                    "description": "Whether to use CoinGecko in demo mode.",
                    "type": "boolean",
                    "default": True,
                },
                "api_key": {"description": "API key for accessing CoinGecko.", "type": "string"},
            },
            "required": ["is_demo", "api_key"],
        },
        "exchange": {
            "description": "Exchange configuration.",
            "$ref": "#/definitions/exchange",
        },
        "log_config": {
            "description": "Logging configuration.",
            "$ref": "#/definitions/logging",
        },
        "experimental": {
            "description": "Experimental configuration.",
            "type": "object",
            "properties": {"block_bad_exchanges": {"type": "boolean"}},
        },
        "pairlists": {
            "description": "Configuration for pairlists.",
            "type": "array",
            "minItems": 1,
            "items": {"type": "object"},
        },
        "max_entry_position_adjustment": {
            "description": f"Maximum entry position adjustment allowed. {__IN_STRATEGY}",
            "type": ["integer", "number"],
            "minimum": -1,
        },
        "add_config_files": {
            "description": "Additional configuration files to load.",
            "type": "array",
            "items": {"type": "string"},
        },
        "orderflow": {
            "description": "Settings related to order flow.",
            "type": "object",
            "properties": {
                "cache_size": {
                    "description": "Size of the cache for order flow data.",
                    "type": "number",
                    "minimum": 1,
                    "default": 1500,
                },
                "max_candles": {
                    "description": "Maximum number of candles to consider.",
                    "type": "number",
                    "minimum": 1,
                    "default": 1500,
                },
                "scale": {
                    "description": "Scale factor for order flow data.",
                    "type": "number",
                    "minimum": 0.0,
                },
                "stacked_imbalance_range": {
                    "description": "Range for stacked imbalance.",
                    "type": "number",
                    "minimum": 0,
                },
                "imbalance_volume": {
                    "description": "Volume threshold for imbalance.",
                    "type": "number",
                    "minimum": 0,
                },
                "imbalance_ratio": {
                    "description": "Ratio threshold for imbalance.",
                    "type": "number",
                    "minimum": 0.0,
                },
            },
            "required": [
                "max_candles",
                "scale",
                "stacked_imbalance_range",
                "imbalance_volume",
                "imbalance_ratio",
            ],
        },
    },
    "definitions": {
        "exchange": {
            "description": "Exchange configuration settings.",
            "type": "object",
            "properties": {
                "name": {"description": "Name of the exchange.", "type": "string"},
                "key": {
                    "description": (
                        f"API key for the exchange. {__VIA_ENV} FREQTRADE__EXCHANGE__KEY"
                    ),
                    "type": "string",
                    "default": "",
                },
                "secret": {
                    "description": (
                        f"API secret for the exchange. {__VIA_ENV} FREQTRADE__EXCHANGE__SECRET"
                    ),
                    "type": "string",
                    "default": "",
                },
                "password": {
                    "description": (
                        "Password for the exchange, if required. "
                        f"{__VIA_ENV} FREQTRADE__EXCHANGE__PASSWORD"
                    ),
                    "type": "string",
                    "default": "",
                },
                "uid": {
                    "description": (
                        "User ID for the exchange, if required. "
                        f"{__VIA_ENV} FREQTRADE__EXCHANGE__UID"
                    ),
                    "type": "string",
                },
                "account_id": {
                    "description": (
                        "Account ID for the exchange, if required. "
                        f"{__VIA_ENV} FREQTRADE__EXCHANGE__ACCOUNT_ID"
                    ),
                    "type": "string",
                },
                "wallet_address": {
                    "description": (
                        "Wallet address for the exchange, if required. "
                        "Usually used by DEX exchanges. "
                        f"{__VIA_ENV} FREQTRADE__EXCHANGE__WALLET_ADDRESS"
                    ),
                    "type": "string",
                },
                "private_key": {
                    "description": (
                        "Private key for the exchange, if required. Usually used by DEX exchanges. "
                        f"{__VIA_ENV} FREQTRADE__EXCHANGE__PRIVATE_KEY"
                    ),
                    "type": "string",
                },
                "pair_whitelist": {
                    "description": "List of whitelisted trading pairs.",
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": True,
                },
                "pair_blacklist": {
                    "description": "List of blacklisted trading pairs.",
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": True,
                },
                "log_responses": {
                    "description": (
                        "Log responses from the exchange."
                        "Useful/required to debug issues with order processing."
                    ),
                    "type": "boolean",
                    "default": False,
                },
                "unknown_fee_rate": {
                    "description": "Fee rate for unknown markets.",
                    "type": "number",
                },
                "outdated_offset": {
                    "description": "Offset for outdated data in minutes.",
                    "type": "integer",
                    "minimum": 1,
                },
                "markets_refresh_interval": {
                    "description": "Interval for refreshing market data in minutes.",
                    "type": "integer",
                    "default": 60,
                },
                "ccxt_config": {"description": "CCXT configuration settings.", "type": "object"},
                "ccxt_async_config": {
                    "description": (
                        "CCXT asynchronous configuration settings."
                        "Usually ccxt_config should be used instead."
                    ),
                    "type": "object",
                },
                "ccxt_sync_config": {
                    "description": (
                        "CCXT synchronous configuration settings. "
                        "Usually ccxt_config should be used instead."
                    ),
                    "type": "object",
                },
            },
            "required": ["name"],
        },
        "logging": {
            "type": "object",
            "properties": {
                "version": {"type": "number", "const": 1},
                "formatters": {
                    "type": "object",
                    # In theory the below, but can be more flexible
                    # based on logging.config documentation
                    # "additionalProperties": {
                    #     "type": "object",
                    #     "properties": {
                    #         "format": {"type": "string"},
                    #         "datefmt": {"type": "string"},
                    #     },
                    #     "required": ["format"],
                    # },
                },
                "handlers": {"type": "object"},
                "root": {"type": "object"},
            },
            "required": ["version", "formatters", "handlers", "root"],
        },
        "external_message_consumer": {
            "description": "Configuration for external message consumer.",
            "type": "object",
            "properties": {
                "enabled": {
                    "description": "Whether the external message consumer is enabled.",
                    "type": "boolean",
                    "default": False,
                },
                "producers": {
                    "description": "List of producers for the external message consumer.",
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "description": "Name of the producer.",
                                "type": "string",
                            },
                            "host": {
                                "description": "Host of the producer.",
                                "type": "string",
                            },
                            "port": {
                                "description": "Port of the producer.",
                                "type": "integer",
                                "default": 8080,
                                "minimum": 0,
                                "maximum": 65535,
                            },
                            "secure": {
                                "description": "Whether to use SSL to connect to the producer.",
                                "type": "boolean",
                                "default": False,
                            },
                            "ws_token": {
                                "description": "WebSocket token for the producer.",
                                "type": "string",
                            },
                        },
                        "required": ["name", "host", "ws_token"],
                    },
                },
                "wait_timeout": {
                    "description": "Wait timeout in seconds.",
                    "type": "integer",
                    "minimum": 0,
                },
                "sleep_time": {
                    "description": "Sleep time in seconds before retrying to connect.",
                    "type": "integer",
                    "minimum": 0,
                },
                "ping_timeout": {
                    "description": "Ping timeout in seconds.",
                    "type": "integer",
                    "minimum": 0,
                },
                "remove_entry_exit_signals": {
                    "description": "Remove signal columns from the dataframe (set them to 0)",
                    "type": "boolean",
                    "default": False,
                },
                "initial_candle_limit": {
                    "description": "Initial candle limit.",
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 1500,
                    "default": 1500,
                },
                "message_size_limit": {
                    "description": "Message size limit in megabytes.",
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 8,
                },
            },
            "required": ["producers"],
        },
    },
}

SCHEMA_TRADE_REQUIRED = [
    "exchange",
    "timeframe",
    "max_open_trades",
    "stake_currency",
    "stake_amount",
    "tradable_balance_ratio",
    "last_stake_amount_min_ratio",
    "dry_run",
    "dry_run_wallet",
    "exit_pricing",
    "entry_pricing",
    "stoploss",
    "minimal_roi",
    "pairlists",
    "internals",
    "dataformat_ohlcv",
    "dataformat_trades",
]

SCHEMA_BACKTEST_REQUIRED = [
    "exchange",
    "stake_currency",
    "stake_amount",
    "pairlists",
    "dry_run_wallet",
    "dataformat_ohlcv",
    "dataformat_trades",
]
SCHEMA_BACKTEST_REQUIRED_FINAL = [
    *SCHEMA_BACKTEST_REQUIRED,
    "stoploss",
    "minimal_roi",
    "max_open_trades",
]

SCHEMA_MINIMAL_REQUIRED = [
    "exchange",
    "dry_run",
    "dataformat_ohlcv",
    "dataformat_trades",
]
SCHEMA_MINIMAL_WEBSERVER = [*SCHEMA_MINIMAL_REQUIRED]
