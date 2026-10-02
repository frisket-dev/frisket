from contextlib import closing

import pytest

from frisket.actions.types import TableError
from frisket.engine.executor.file_inventory_read import (
    AdmittedFileInventoryReader,
    FileInventoryAdmission,
)
from frisket.engine.executor.import_blob_stage import AdmittedImportBlobStager
from frisket.engine.store import Project
from frisket.engine.store.import_inventory import ImportInventory


def test_page_admission_bounds_occurrences_without_touching_originals(
    tmp_path, monkeypatch
):
    with (
        closing(Project.create(tmp_path / "p.frisket")) as project,
        ImportInventory(tmp_path / "inventory.db") as inventory,
        AdmittedImportBlobStager() as stager,
    ):
        digest = project.blob_store.put(b"original")
        inventory.append(
            {
                "logical_path": f"nested/{i}.pdf",
                "mime": "application/pdf",
                "sha256": digest,
                "size": 8,
                "kind": "files",
            }
            for i in range(5)
        )
        inventory.seal()

        def unexpected(*args, **kwargs):
            pytest.fail("page admission must not copy/download verified originals")

        monkeypatch.setattr(project.blob_store, "put_path", unexpected)
        monkeypatch.setattr(project.blob_store, "materialize", unexpected)
        admission = FileInventoryAdmission(
            "owned-ref", inventory, project.blob_store, after=0, limit=4, byte_budget=16
        )
        first = AdmittedFileInventoryReader(admission, stager)
        files = first.files("owned-ref")
        assert [file.filename for file in files] == ["0.pdf", "1.pdf"]
        assert len({file.file for file in files}) == 2
        assert (first.next_cursor, first.admitted_bytes, first.complete) == (
            2,
            16,
            False,
        )
        assert len(first.facts) == 1
        with pytest.raises(TableError, match="only once"):
            first.files("owned-ref")
        last = AdmittedFileInventoryReader(
            FileInventoryAdmission("owned-ref", inventory, project.blob_store, after=4),
            stager,
        )
        assert [file.filename for file in last.files("owned-ref")] == ["4.pdf"]
        assert last.complete


def test_unsealed_empty_page_is_not_complete_and_ref_cannot_select_inventory(tmp_path):
    with (
        closing(Project.create(tmp_path / "p.frisket")) as project,
        ImportInventory(tmp_path / "inventory.db") as inventory,
        AdmittedImportBlobStager() as stager,
    ):
        reader = AdmittedFileInventoryReader(
            FileInventoryAdmission("owned-ref", inventory, project.blob_store, after=0),
            stager,
        )
        with pytest.raises(TableError, match="matching host admission"):
            reader.files("../other-project/inventory.db")
        assert reader.files("owned-ref") == ()
        assert not reader.complete
        assert reader.next_cursor == 0
        with pytest.raises(TableError, match="matching host admission"):
            AdmittedFileInventoryReader().files("owned-ref")
