from __future__ import annotations

from frisket.server.services.project_qa_output_scope import OutputReadGrant
from frisket.server.services.project_qa_tools import _retrieval_scope


def test_ask_scope_keeps_whole_rows_and_exact_cells_as_a_union() -> None:
    scope = _retrieval_scope(
        5,
        {7, 8},
        {(8, 11), (9, 12)},
        (
            OutputReadGrant(
                receipt_id="receipt:1",
                sheet_id=5,
                row_ids=frozenset({10, 11}),
                column_ids=frozenset({13}),
            ),
        ),
    )

    assert scope.sheets is not None
    assert [(item.row_ids, item.column_ids) for item in scope.sheets] == [
        ((7, 8), None),
        ((9,), (12,)),
        ((10, 11), (13,)),
    ]
