"""Shared selection reads must retain the caller's snapshot and full membership."""

import pytest

from frisket.engine.store import Project
from frisket.retrieval.contracts import (
    RetrievalScope,
    SearchRequest,
    SheetScope,
    TextQuery,
)
from frisket.retrieval.query import (
    PinnedRetrievalReader,
    RetrievalQueryError,
    RetrievalQueryService,
)
from frisket.search_index import drain_index


@pytest.fixture
def document_rows(tmp_path):
    project = Project.create(tmp_path / "selection.frisket")
    sheet = project.add_sheet("records")
    column = project.add_column(sheet, "text")
    rows = project.add_rows(
        sheet, [{"text": "budget hearing"}, {"text": "roads"}], {"text": column}
    )
    drain_index(project)
    try:
        yield project, sheet, column, rows
    finally:
        project.close()


def request(sheet, *, limit=1000000):
    return SearchRequest(
        scope=RetrievalScope((SheetScope(sheet),)),
        query=TextQuery("budget"),
        group_by="row",
        limit=limit,
    )


def test_retrieval_borrows_snapshot_and_does_not_close_or_advance_it(document_rows):
    project, sheet, column, rows = document_rows
    with project.read_snapshot() as snapshot:
        assert snapshot.get_values(sheet, column)[rows[0]] == "budget hearing"
        project.apply_edits(
            [{"row_id": rows[0], "column_id": column, "value": "nothing"}]
        )
        with PinnedRetrievalReader(snapshot) as reader:
            assert reader.db is snapshot.db
        assert snapshot.get_values(sheet, column)[rows[0]] == "budget hearing"
        hits = list(RetrievalQueryService(snapshot).iter_row_matches(request(sheet)))
        assert [hit["row_id"] for hit in hits] == rows[:1]
        # Closing nested readers must preserve both outer transaction and attachment.
        assert snapshot.db.in_transaction
        assert (
            list(RetrievalQueryService(snapshot).iter_row_matches(request(sheet)))
            == hits
        )
    with project.read_snapshot() as current:
        with pytest.raises(RetrievalQueryError, match="incomplete"):
            list(RetrievalQueryService(current).iter_row_matches(request(sheet)))


def test_scoped_membership_is_not_global_top_k(document_rows):
    project, sheet, column, rows = document_rows
    other = project.add_sheet("other")
    other_column = project.add_column(other, "text")
    project.add_rows(
        other, [{"text": "budget"} for _ in range(110)], {"text": other_column}
    )
    extra = project.add_rows(
        sheet, [{"text": "budget record"} for _ in range(510)], {"text": column}
    )
    drain_index(project)
    with project.read_snapshot() as snapshot:
        stream = RetrievalQueryService(snapshot).iter_row_matches(request(sheet))
        assert iter(stream) is stream
        hits = list(stream)
        assert {hit["row_id"] for hit in hits} == set(rows[:1] + extra)
        assert all(hit["sheet_id"] == sheet for hit in hits)
        assert (
            len(
                list(
                    RetrievalQueryService(snapshot).iter_row_matches(
                        request(sheet, limit=1)
                    )
                )
            )
            == 1
        )
