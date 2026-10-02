from __future__ import annotations

import io
from contextlib import contextmanager

import pytest
from pydantic import ValidationError

from frisket.actions.import_inventory_types import InventoryFile
from frisket.actions.import_media import ImportFilesParams, file_columns, import_files
from frisket.actions.types import StagedFile


class _Files:
    def __init__(self, payload: bytes = b"direct") -> None:
        self.payload = payload
        self.opened: list[str] = []

    @contextmanager
    def open_binary(self, path: str):
        self.opened.append(path)
        with io.BytesIO(self.payload) as stream:
            yield stream


class _Blobs:
    def __init__(self) -> None:
        self.staged = 0

    def stage(self, stream, *, filename, mime, role=None):
        del filename, mime, role
        payload = stream.read()
        self.staged += 1
        return StagedFile(size=len(payload))


class _Inventory:
    def __init__(self, items=()) -> None:
        self.items = tuple(items)
        self.refs: list[str] = []

    def files(self, ref: str):
        self.refs.append(ref)
        yield from self.items


def test_inventory_mode_emits_existing_file_row_shape_and_generic_schema():
    items = [
        InventoryFile("first.bin", StagedFile(size=3)),
        InventoryFile("second.bin", StagedFile(size=7)),
    ]
    inventory = _Inventory(items)
    files, blobs = _Files(), _Blobs()
    params = ImportFilesParams(inventory_ref="inventory:opaque")

    assert [(column.key, column.type) for column in file_columns(params)] == [
        ("filename", "text"),
        ("media", "file"),
        ("size", "integer"),
    ]
    rows = [
        row.output.root for row in import_files(params, files, blobs, inventory).rows
    ]
    assert rows == [
        {"filename": "first.bin", "media": items[0].file, "size": 3},
        {"filename": "second.bin", "media": items[1].file, "size": 7},
    ]
    assert inventory.refs == ["inventory:opaque"]
    assert files.opened == [] and blobs.staged == 0


def test_direct_mode_keeps_staging_path_without_reading_inventory():
    class RefusingInventory:
        def files(self, ref: str):
            raise AssertionError(f"inventory unexpectedly read: {ref}")

    files, blobs = _Files(b"direct bytes"), _Blobs()
    params = ImportFilesParams(files=[{"path": "/admitted/report.pdf"}])
    rows = [
        row.output.root
        for row in import_files(params, files, blobs, RefusingInventory()).rows
    ]
    assert rows[0]["filename"] == "report.pdf"
    assert rows[0]["size"] == len(b"direct bytes")
    assert files.opened == ["/admitted/report.pdf"]
    assert blobs.staged == 1


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"files": [], "inventory_ref": "inventory:opaque"},
        {"files": [{"path": "/admitted/file"}], "inventory_ref": "inventory:opaque"},
    ],
)
def test_import_files_params_require_exactly_one_source(values):
    with pytest.raises(ValidationError):
        ImportFilesParams.model_validate(values)
