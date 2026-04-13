def stoploss_from_open(
    open_relative_stop: float, current_profit: float, is_short: bool = False, leverage: float = 1.0
) -> float:
    """
    Given the current profit, and a desired stop loss value relative to the trade entry price,
    return a stop loss value that is relative to the current price, and which can be
    returned from `custom_stoploss`.

    The requested stop can be positive for a stop above the open price, or negative for
    a stop below the open price. The return value is always >= 0.
    `open_relative_stop` will be considered as adjusted for leverage if leverage is provided..

    Returns 0 if the resulting stop price would be above/below (longs/shorts) the current price

    :param open_relative_stop: Desired stop loss percentage, relative to the open price,
                               adjusted for leverage
    :param current_profit: The current profit percentage
    :param is_short: When true, perform the calculation for short instead of long
    :param leverage: Leverage to use for the calculation
    :return: Stop loss value relative to current price
    """

    # formula is undefined for current_profit -1 (longs) or 1 (shorts), return maximum value
    _current_profit = current_profit / leverage
    if (_current_profit == -1 and not is_short) or (is_short and _current_profit == 1):
        return 1

    if is_short is True:
        stoploss = -1 + ((1 - open_relative_stop / leverage) / (1 - _current_profit))
    else:
        stoploss = 1 - ((1 + open_relative_stop / leverage) / (1 + _current_profit))

    # negative stoploss values indicate the requested stop price is higher/lower
    # (long/short) than the current price
    return max(stoploss * leverage, 0.0)


def stoploss_from_absolute(
    stop_rate: float, current_rate: float, is_short: bool = False, leverage: float = 1.0
) -> float:
    """
    Given current price and desired stop price, return a stop loss value that is relative to current
    price.

    The requested stop can be positive for a stop above the open price, or negative for
    a stop below the open price. The return value is always >= 0.

    Returns 0 if the resulting stop price would be above the current price.

    :param stop_rate: Stop loss price.
    :param current_rate: Current asset price.
    :param is_short: When true, perform the calculation for short instead of long
    :param leverage: Leverage to use for the calculation
    :return: Positive stop loss value relative to current price
    """

    # formula is undefined for current_rate 0, return maximum value
    if current_rate == 0:
        return 1

    stoploss = 1 - (stop_rate / current_rate)
    if is_short:
        stoploss = -stoploss

    # negative stoploss values indicate the requested stop price is higher/lower
    # (long/short) than the current price
    return max(stoploss, 0.0) * leverage
