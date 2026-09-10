"""Find every grounded occurrence in the selected source rows."""

from frisket.actions.core import ActionCategory, action
from frisket.actions.find_types import (
    FindOptions,
    FindScanner,
    FindSourceColumn,
    PreparedFind,
)


class FindParams(FindOptions):
    source: FindSourceColumn


def find(params: FindParams, scanner: FindScanner) -> PreparedFind:
    return scanner.prepare(
        params.source,
        options=FindOptions.model_validate(params.model_dump(exclude={"source"})),
    )


FIND = action(
    name="find",
    title="Find grounded occurrences",
    description="Scan every source window and create evidence-linked findings.",
    category=ActionCategory.TEXT,
    run=find,
    examples=(
        FindParams(
            source="transcript",
            model="anthropic/claude-haiku-4-5",
            instruction="Find every mention of trade policy.",
        ),
    ),
)
