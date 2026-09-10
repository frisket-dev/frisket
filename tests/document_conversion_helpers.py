"""Bind the real converter when testing individual transport/sandbox seams."""

from frisket.actions.types import Row
from frisket.engine.executor.document_convert import AdmittedDocumentConverter
from frisket.ops.base import OpContext


def bound_document_converter(ctx=None, *, engine="markitdown"):
    ctx = ctx if ctx is not None else OpContext()
    return AdmittedDocumentConverter(ctx, engine=engine).bind_row(
        Row({}), sheet_id=1, row_id=1, sources={}, ctx=ctx
    )
