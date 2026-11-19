"""Recover selected cells using their admitted source generation."""

from typing import Any
from pydantic import Field
from frisket.actions.types import RetainedResultPublisher, ReappliedResults

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
    examples=(BackfillParams(column=ColumnRef[Any]("answer")),),
)


class ReapplyParams(ActionParams):
    source_run_id: int = Field(gt=0, strict=True)


def reapply(
    params: ReapplyParams, results: RetainedResultPublisher
) -> ReappliedResults:
    return results.reapply(params.source_run_id)


REAPPLY = action(
    name="reapply",
    title="Reapply saved results",
    description="Publish retained results as a new undoable project change.",
    category=ActionCategory.CLEANUP,
    run=reapply,
    examples=(ReapplyParams(source_run_id=1),),
)


class RetryParams(ActionParams):
    source_run_id: int = Field(gt=0, strict=True)


def retry(params: RetryParams, runs: RunBackfiller) -> PreparedBackfill:
    return runs.retry(params.source_run_id)


RETRY = action(
    name="retry",
    title="Retry unfinished execution",
    description="Authorize a fresh attempt over unfinished original rows using current inputs.",
    category=ActionCategory.CONVERT,
    run=retry,
    examples=(RetryParams(source_run_id=1),),
)
