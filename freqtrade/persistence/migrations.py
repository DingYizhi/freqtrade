import logging

from sqlalchemy import Engine, inspect, select, text, update

from freqtrade.exceptions import OperationalException
from freqtrade.persistence.trade_model import Order, Trade


logger = logging.getLogger(__name__)


def has_column(columns: list, searchname: str) -> bool:
    return len(list(filter(lambda column: column["name"] == searchname, columns))) == 1


def ensure_supported_schema(engine: Engine, previous_tables: list[str]) -> None:
    """
    This fork no longer carries the large legacy migration stack.
    Existing databases must already match the current schema.
    """
    if "orders" not in previous_tables and "trades" in previous_tables:
        raise OperationalException(
            "Legacy database migration support has been removed from this backtesting fork. "
            "Please migrate the database with upstream Freqtrade first, or start with a fresh database."
        )

    inspector = inspect(engine)
    cols_trades = inspector.get_columns("trades")
    cols_pairlocks = inspector.get_columns("pairlocks")

    missing_columns: list[str] = []
    if not has_column(cols_trades, "record_version"):
        missing_columns.append("trades.record_version")
    if not has_column(cols_pairlocks, "side"):
        missing_columns.append("pairlocks.side")

    if missing_columns:
        missing = ", ".join(missing_columns)
        raise OperationalException(
            "Legacy database migration support has been removed from this backtesting fork. "
            f"Unsupported schema detected: {missing}. "
            "Please migrate the database with upstream Freqtrade first, or start with a fresh database."
        )


def set_sqlite_to_wal(engine: Engine) -> None:
    if engine.name == "sqlite" and str(engine.url) != "sqlite://":
        with engine.begin() as connection:
            connection.execute(text("PRAGMA journal_mode=wal"))


def fix_old_dry_orders(engine: Engine) -> None:
    with engine.begin() as connection:
        stmt = (
            update(Order)
            .where(
                Order.ft_is_open.is_(True),
                Order.ft_order_side == "stoploss",
                Order.order_id.like("dry%"),
            )
            .values(ft_is_open=False)
        )
        connection.execute(stmt)

        stmt = (
            update(Order)
            .where(
                Order.ft_is_open.is_(True),
                Order.ft_trade_id.not_in(select(Trade.id).where(Trade.is_open.is_(True))),
                Order.ft_order_side != "stoploss",
                Order.order_id.like("dry%"),
            )
            .values(ft_is_open=False)
        )
        connection.execute(stmt)


def fix_wrong_max_stake_amount(engine: Engine) -> None:
    with engine.begin() as connection:
        stmt = (
            update(Trade)
            .where(
                Trade.record_version < 2,
                Trade.leverage > 1,
                Trade.is_open.is_(False),
                Trade.max_stake_amount != 0,
            )
            .values(max_stake_amount=Trade.max_stake_amount / Trade.leverage, record_version=2)
        )
        connection.execute(stmt)


def check_migrate(engine: Engine, decl_base, previous_tables: list[str]) -> None:
    """
    Validate that the existing database already matches the supported schema,
    then apply the small in-place fixes still relevant for backtesting.
    """
    ensure_supported_schema(engine, previous_tables)
    set_sqlite_to_wal(engine)
    fix_old_dry_orders(engine)
    fix_wrong_max_stake_amount(engine)
