"""Convert documents to Markdown with an engine-selectable row action."""

from __future__ import annotations

from pydantic import field_validator

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.document_types import (
    DEFAULT_DOCUMENT_ENGINE,
    ConvertedDocument,
    DocumentColumn,
    DocumentConverter,
)
from frisket.actions.types import ActionParams, EngineRef, Row, RowResult
from frisket.contracts.actions.schemas._engines import (
    TO_MARKDOWN_ENGINE_TABLE,
    TO_MARKDOWN_DEAD_ENGINE_REPLACEMENTS,
    dead_engine_rejection,
    engine_ids,
    execution_alias_map,
    symbolic_engine_names,
)

_SIDECAR_ENGINES = frozenset(engine_ids(TO_MARKDOWN_ENGINE_TABLE, tier="sidecar"))
_ENGINE_ALIASES = execution_alias_map(TO_MARKDOWN_ENGINE_TABLE)


class ToMarkdownParams(ActionParams):
    source: DocumentColumn
    engine: EngineRef[DocumentConverter] = EngineRef[DocumentConverter](
        DEFAULT_DOCUMENT_ENGINE
    )

    @field_validator("engine")
    @classmethod
    def _supported_engine(cls, value: EngineRef[DocumentConverter]):
        if value.root not in symbolic_engine_names(TO_MARKDOWN_ENGINE_TABLE):
            reason = dead_engine_rejection(
                TO_MARKDOWN_DEAD_ENGINE_REPLACEMENTS, value.root
            )
            if reason:
                raise ValueError(f"invalid_to_markdown_engine: {reason}")
            raise ValueError("invalid_to_markdown_engine")
        return value


def _to_markdown_outputs(params: ToMarkdownParams) -> tuple[str, ...]:
    engine = _ENGINE_ALIASES.get(params.engine.root, params.engine.root)
    return ("markdown", "ocr_used") if engine in _SIDECAR_ENGINES else ("markdown",)


async def to_markdown(
    params: ToMarkdownParams, row: Row, converter: DocumentConverter
) -> RowResult[ConvertedDocument]:
    return RowResult(output=await converter.convert(row, params.source))


TO_MARKDOWN = action(
    name="to_markdown",
    title="To markdown",
    description=(
        "Convert documents (html, docx, pptx, xlsx, text PDFs) to markdown. "
        "Engine-selectable: local 'markitdown' (default, offline) or "
        "'trafilatura_html' (HTML extraction), 'docling'/"
        "'chandra' (sidecar, layout-aware / experimental document VLM), or "
        "'datalab' (hosted Marker API, pay-per-call)."
    ),
    category=ActionCategory.EXTRACT,
    examples=(ToMarkdownParams(source="document"),),
    run=map_rows(to_markdown, active_outputs=_to_markdown_outputs),
)
