import logging
from collections.abc import Callable
from typing import Any, TypeVar, cast

from freqtrade.exceptions import StrategyError


logger = logging.getLogger(__name__)


F = TypeVar("F", bound=Callable[..., Any])

# Cache for wrapped functions: (f, message, default_retval, supress_error) -> wrapper
_wrapper_cache: dict[tuple, Any] = {}


def __format_traceback(error: Exception) -> str:
    """Format the traceback of an exception into a formatted string."""
    tb = error.__traceback__
    try:
        while tb:
            if tb.tb_frame.f_code.co_filename == __file__:
                # Skip frames from this file
                tb = tb.tb_next
                continue
            return f"{tb.tb_frame.f_code.co_qualname}:{tb.tb_lineno}"
    except Exception:
        return "<unavailable>"
    return ""


def strategy_safe_wrapper(f: F, message: str = "", default_retval=None, supress_error=False) -> F:
    """
    Wrapper around user-provided methods and functions.
    Caches all exceptions and returns either the default_retval (if it's not None) or raises
    a StrategyError exception, which then needs to be handled by the calling method.
    """
    cache_key = (id(f), message, id(default_retval), supress_error)
    cached = _wrapper_cache.get(cache_key)
    if cached is not None:
        return cached

    # Pre-compute whether this is a user strategy method (not IStrategy base)
    is_user_method = not (getattr(f, "__qualname__", "")).startswith("IStrategy.")

    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except ValueError as error:
            traceback = __format_traceback(error)
            name = f.__name__ if hasattr(f, "__name__") else str(f)
            logger.warning(
                f"{message}Strategy caused the following exception: {repr(error)} in "
                f"{traceback}, calling {name}",
            )
            if default_retval is None and not supress_error:
                raise StrategyError(str(error)) from error
            return default_retval
        except Exception as error:
            logger.exception(f"{message}Unexpected error {repr(error)} calling {f}")
            if default_retval is None and not supress_error:
                raise StrategyError(str(error)) from error
            return default_retval

    result = cast(F, wrapper)
    _wrapper_cache[cache_key] = result
    return result
