"""header_row_finder (hook locate): the header is the first row whose first non-empty cell is the declared text.

For a file whose title and notes sit above the table and move from one release to the next. The match is exact
(after trimming spaces), only the first ``max_scan`` rows are read, and a file with no such row, or with it more
than once, is refused rather than read from a guessed row. No value is changed.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import Field

from health_data_registry.adapters.extensions import Extension, ExtensionLayoutRefused, Location, register
from health_data_registry.source_config import Strict


class Args(Strict):
    first_cell: str = Field(min_length=1, max_length=200)
    max_scan: int = Field(default=50, ge=1, le=1_000)


def locate(args: Args, grid: Sequence[Sequence[str]]) -> Location:
    hits = []
    for i, row in enumerate(grid[: args.max_scan]):
        first = next((c.strip() for c in row if c.strip()), None)
        if first == args.first_cell.strip():
            hits.append(i)
    if not hits:
        raise ExtensionLayoutRefused(
            f"no row in the first {args.max_scan} starts with {args.first_cell!r}, so the header can't be found"
        )
    if len(hits) > 1:
        raise ExtensionLayoutRefused(
            f"rows {', '.join(str(h + 1) for h in hits)} all start with {args.first_cell!r}"
        )
    return Location(header_row=hits[0])


EXTENSION = register(
    Extension(
        name="header_row_finder",
        hook="locate",
        args_model=Args,
        run=locate,
        summary="The header is the one row, among the first few, whose first filled cell is the declared text.",
    )
)
