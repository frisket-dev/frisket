"""An installed table Action that aggregates the request's selected rows.

The scope's rows arrive through `SheetRowsReader`, so selection stays where it
belongs: `Request.scope` owns sheet_id and row_ids, and the Params model
declares only which columns to read. Each emitted `TableRow` carries the
lineage tokens of the rows it summarized, and the host derives the child
sheet's parent and contributor lineage from those.
"""

from __future__ import annotations

from pydantic import BaseModel

from frisket.actions.core import ActionCategory, action, create_sheet
from frisket.sdk import ColumnRef, SheetRowsReader, TableResult, TableRow
from frisket.actions.types import ActionParams
from frisket.plugins.sdk import Plugin


class StarSummaryParams(ActionParams):
    group_by: ColumnRef[str]
    value_column: ColumnRef[float]
    label_column: ColumnRef[str]
    # Opt-in defect switch: emits a row whose declared int output holds a
    # string, so the host's publication validation is exercised for real
    # instead of being faked over a transport that no longer exists.
    emit_invalid_row: bool = False


class StarSummaryOutput(BaseModel):
    constellation: str
    star_count: int
    average_rating: float
    top_star: str
    source_row_count: int


def summarize_stars(
    params: StarSummaryParams, rows: SheetRowsReader
) -> TableResult[StarSummaryOutput]:
    groups: dict[str, list] = {}
    for item in rows.read():
        constellation = str(params.group_by.read(item.row) or "").strip() or "(blank)"
        groups.setdefault(constellation, []).append(item)

    summaries = []
    for constellation in sorted(groups):
        stars = groups[constellation]
        ratings = [float(params.value_column.read(star.row)) for star in stars]
        top = max(
            stars,
            key=lambda star: (
                float(params.value_column.read(star.row)),
                # Ties break on the label, matching the pre-cutover ordering.
                [-ord(character) for character in params.label_column.read(star.row)],
            ),
        )
        output = StarSummaryOutput(
            constellation=constellation,
            star_count=len(stars),
            average_rating=round(sum(ratings) / len(ratings), 2),
            top_star=params.label_column.read(top.row),
            source_row_count=len(stars),
        )
        if params.emit_invalid_row:
            output = StarSummaryOutput.model_construct(
                **{**dict(output), "star_count": "not an integer"}
            )
        summaries.append(
            TableRow(
                output=output,
                sources=tuple(star.source for star in stars),
            )
        )
    return TableResult(rows=tuple(summaries))


SUMMARIZE_STARS = action(
    name="summarize_stars",
    title="Summarize star ratings",
    description="Aggregates the selected rows into a star summary sheet.",
    category=ActionCategory.CONVERT,
    run=create_sheet(summarize_stars),
)

plugin = Plugin(
    id="demo.star_summary",
    version="0.1.0",
    capabilities=["plugin:trusted_local_backend"],
    actions=(SUMMARIZE_STARS,),
)
