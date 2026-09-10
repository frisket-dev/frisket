"""Summarize selected rows into one contributed row per group."""

from pydantic import Field, field_validator

from frisket.actions.core import ActionCategory, action
from frisket.actions.model_rows import FILE_SOURCE_CONVERSION_HINT
from frisket.actions.group_summary_types import (
    GroupSummarizer,
    GroupSummaryColumn,
    GroupSummaryOptions,
    PreparedGroupSummary,
)


class GroupSummaryParams(GroupSummaryOptions):
    source: list[GroupSummaryColumn] = Field(
        min_length=1, description=FILE_SOURCE_CONVERSION_HINT
    )

    @field_validator("source")
    @classmethod
    def unique_source(cls, value):
        if len({column.name for column in value}) != len(value):
            raise ValueError("source columns must be distinct")
        return value


def group_summary(
    params: GroupSummaryParams, summarizer: GroupSummarizer
) -> PreparedGroupSummary:
    return summarizer.prepare(
        params.source,
        options=GroupSummaryOptions.model_validate(
            params.model_dump(exclude={"source"})
        ),
    )


GROUP_SUMMARY = action(
    name="group_summary",
    title="Summarize grouped rows",
    description="Synthesize one faithful summary per group into a new sheet.",
    category=ActionCategory.TEXT,
    run=group_summary,
    examples=(
        GroupSummaryParams(
            source=["story"],
            model="anthropic/claude-haiku-4-5",
            instruction="Summarize these stories.",
        ),
    ),
)
