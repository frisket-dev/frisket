from __future__ import annotations

import asyncio
from contextlib import closing

import pytest

from frisket.actions.types import ColumnRef, Row, RowError
from frisket.engine.executor.document_convert import (
    AdmittedDocumentConverter,
    _BoundDocumentConverter,
)
from frisket.engine.store import Project
from frisket.ops.base import OpContext


def test_retyping_admitted_text_cannot_authorize_path_read(tmp_path, monkeypatch):
    secret = tmp_path / "secret.txt"
    secret.write_text("not document input")
    dispatched = []

    async def convert(self, path, scratch):
        dispatched.append(path.read_text())
        return path.read_text()

    monkeypatch.setattr(_BoundDocumentConverter, "_convert_markitdown", convert)
    with closing(Project.create(tmp_path / "p.frisket")) as project:
        sheet = project.add_sheet("docs")
        column = project.add_column(sheet, "source", type="text")
        [row_id] = project.add_rows(
            sheet, [{"source": str(secret)}], {"source": column}
        )
        row = Row({"source": str(secret)})
        ctx = OpContext(project=project, extras={"row_id": row_id})
        owner = AdmittedDocumentConverter(ctx, engine="markitdown")
        bound = owner.bind_row(
            row,
            sheet_id=sheet,
            row_id=row_id,
            ctx=ctx,
            sources={
                "source": {
                    "column_id": column,
                    "column_type": "text",
                    "value": str(secret),
                    "value_ref": None,
                }
            },
        )
        # Retyping must not upgrade an already admitted literal into a path.
        project.db.execute("UPDATE columns SET type='file' WHERE id=?", (column,))
        project.db.commit()
        with pytest.raises(RowError):
            asyncio.run(bound.convert(row, ColumnRef("source")))
        assert dispatched == []
