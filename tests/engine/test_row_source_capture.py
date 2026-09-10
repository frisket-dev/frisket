from types import SimpleNamespace

import pytest

from frisket.engine.runner.row_inputs import row_values


@pytest.mark.parametrize("template", ["", "Video: {{video}}"])
def test_source_snapshot_is_captured_with_the_values_in_one_read(template):
    calls = []
    source = {"blob_hash": "source-blob"}
    ref = {"kind": "raw", "id": 31}

    def read_with_refs(sheet_id, column_id, *, row_ids):
        calls.append((sheet_id, column_id, row_ids))
        return {7: source}, {7: ref}

    project = SimpleNamespace(get_values_with_refs=read_with_refs)
    recipe = SimpleNamespace(
        capture_source_cells=True, source_columns=lambda spec: ["video"]
    )
    values = row_values(
        project,
        recipe,
        {"sheet_id": 2, "input_template": template},
        {"video": 4},
        7,
        for_model=False,
        column_types={"video": "video"},
    )
    assert calls == [(2, 4, [7])]
    assert values.source_cells == {
        "video": {
            "column_id": 4,
            "column_type": "video",
            "value": source,
            "value_ref": ref,
        }
    }
    if not template:
        assert values["video"] == source


def test_unrelated_row_reader_does_not_request_source_references():
    project = SimpleNamespace(get_values=lambda *args, **kwargs: {7: "plain"})
    recipe = SimpleNamespace(source_columns=lambda spec: ["text"])
    values = row_values(
        project,
        recipe,
        {"sheet_id": 2},
        {"text": 4},
        7,
        for_model=False,
        column_types={"text": "text"},
    )
    assert values == {"text": "plain"}
    assert values.source_cells is None
