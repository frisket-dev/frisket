"""Recover selected cells using their admitted source generation."""

from typing import Any

from frisket.actions.core import ActionCategory, action
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    PreparedBackfill,
    RunBackfiller,
)


class BackfillParams(ActionParams):
    column: ColumnRef[Any]


def backfill(params: BackfillParams, runs: RunBackfiller) -> PreparedBackfill:
    return runs.prepare(params.column)


BACKFILL = action(
    name="backfill",
    title="Backfill column results",
    description=(
        "Create a fresh generation for missing results, or deliberately retry "
        "the selected rows from one source generation."
    ),
    category=ActionCategory.CONVERT,
    run=backfill,
    form="run_backfill",
    examples=(BackfillParams(column="answer"),),
)
