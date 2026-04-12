from collections.abc import Sequence
from typing import Any, TypeAlias

from rich.table import Column, Table
from rich.text import Text

from freqtrade.loggers.rich_console import get_rich_console


TextOrString: TypeAlias = str | Text


def print_rich_table(
    tabular_data: Sequence[dict[str, Any] | Sequence[TextOrString]],
    headers: Sequence[str],
    summary: str | None = None,
    *,
    justify="right",
    table_kwargs: dict[str, Any] | None = None,
) -> None:
    table = Table(
        *[c if isinstance(c, Column) else Column(c, justify=justify) for c in headers],
        title=summary,
        **(table_kwargs or {}),
    )

    for row in tabular_data:
        if isinstance(row, dict):
            table.add_row(
                *[
                    row[header] if isinstance(row[header], Text) else str(row[header])
                    for header in headers
                ]
            )

        else:
            row_to_add: list[str | Text] = [r if isinstance(r, Text) else str(r) for r in row]
            table.add_row(*row_to_add)

    console = get_rich_console()
    console.print(table)


def _format_value(value: Any, *, floatfmt: str) -> str:
    if isinstance(value, float):
        return f"{value:{floatfmt}}"
    return str(value)
