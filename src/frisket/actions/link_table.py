"""Materialize reviewed semantic matches without repeating semantic compute."""

from pydantic import Field

from frisket.actions.core import ActionCategory, action, create_sheet
from frisket.actions.semantic_match_types import (
    SemanticJoinSource,
    SemanticMatchReader,
    SemanticMatchValues,
)
from frisket.actions.types import ActionParams, TableResult, TableRow


class LinkTableParams(ActionParams):
    source: SemanticJoinSource
    include_unmatched: bool = Field(default=False, strict=True)


def link_table(
    params: LinkTableParams, matches: SemanticMatchReader
) -> TableResult[SemanticMatchValues]:
    return TableResult(
        rows=tuple(
            TableRow(
                output=match.value,
                sources=(match.source, match.target)
                if match.target
                else (match.source,),
                parent=match.source,
            )
            for match in matches.read(
                params.source, include_unmatched=params.include_unmatched
            )
        )
    )


LINK_TABLE = action(
    name="link_table",
    title="Materialize semantic join links",
    description="Create a link table from a completed semantic join and its applied review decisions, without rerunning embeddings.",
    category=ActionCategory.CONVERT,
    run=create_sheet(link_table),
    examples=(
        LinkTableParams(
            source=SemanticJoinSource(
                kind="semantic_join", receipt_id="example-semantic-receipt"
            )
        ),
    ),
)
