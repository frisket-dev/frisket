from contextlib import closing

import pytest

from frisket.engine.executor.import_blob_stage import AdmittedImportBlobStager
from frisket.engine.store import Project, op_log
from frisket.engine.store.import_blobs import prepare_import_blobs, publish_import_blobs


def test_owned_inventory_reuses_bytes_and_preserves_distinct_occurrences(
    tmp_path, monkeypatch
):
    data = b"shared original"
    with closing(Project.create(tmp_path / "owned.frisket")) as project:
        digest = project.blob_store.put(data)
        sheet = project.add_sheet("Documents")
        columns = {"file": project.add_column(sheet, "file", type="file")}
        rows = project.add_rows(sheet, [{"file": None}, {"file": None}], columns)

        def unexpected_copy(*args, **kwargs):
            pytest.fail("owned original must not be copied or downloaded to publish")

        with monkeypatch.context() as patch:
            patch.setattr(project.blob_store, "put_path", unexpected_copy)
            patch.setattr(project.blob_store, "materialize", unexpected_copy)
            with AdmittedImportBlobStager() as stager:
                handles = [
                    stager.admit_owned(
                        project.blob_store,
                        digest=digest,
                        size=len(data),
                        filename=name,
                        mime="application/pdf",
                    )
                    for name in ("a.pdf", "b.pdf")
                ]
                plan = stager.publication_plan(
                    [
                        (row, "file", handle)
                        for row, handle in zip(rows, handles, strict=True)
                    ]
                )
                assert len(plan.blobs) == 2
                assert all(blob.path is None for blob in plan.blobs)
                assert [stager.lower(handle)["filename"] for handle in handles] == [
                    "a.pdf",
                    "b.pdf",
                ]
                prepared = prepare_import_blobs(project, plan)
                with project.db:
                    op = op_log.append_op(
                        project, "import.files", spec={}, commit=False
                    )
                    refs = publish_import_blobs(
                        project,
                        prepared,
                        sheet_id=sheet,
                        column_ids=columns,
                        op_id=op,
                        receipt_id="owned-import",
                    )
                assert [ref["filename"] for ref in refs] == ["a.pdf", "b.pdf"]
        # Closing the invocation only removes its scratch, never canonical bytes.
        assert project.read_blob(digest) == data


def test_owned_blob_reader_is_read_only_and_other_project_is_refused(tmp_path):
    with (
        closing(Project.create(tmp_path / "first.frisket")) as first,
        closing(Project.create(tmp_path / "second.frisket")) as second,
        AdmittedImportBlobStager() as stager,
    ):
        digest = first.blob_store.put(b"original")
        handle = stager.admit_owned(
            first.blob_store,
            digest=digest,
            size=8,
            filename="file.txt",
            mime="text/plain",
        )
        with stager.open_binary(handle) as stream:
            assert stream.read() == b"original"
            with pytest.raises(OSError):
                stream.write(b"change")
        plan = stager.publication_plan([(0, "file", handle)])
        with pytest.raises(ValueError, match="another blob store"):
            prepare_import_blobs(second, plan)
        prepared = prepare_import_blobs(first, plan)
        second.db.execute("BEGIN")
        with pytest.raises(ValueError, match="another blob store"):
            publish_import_blobs(
                second,
                prepared,
                sheet_id=1,
                column_ids={"file": 1},
                op_id=1,
                receipt_id="other",
            )
        second.db.rollback()


@pytest.mark.parametrize("facts", [{"digest": "../blob"}, {"size": -1}, {"size": True}])
def test_owned_inventory_facts_are_validated(tmp_path, facts):
    with (
        closing(Project.create(tmp_path / "invalid.frisket")) as project,
        AdmittedImportBlobStager() as stager,
    ):
        values = dict(digest="a" * 64, size=0, filename="x", mime="text/plain")
        values.update(facts)
        with pytest.raises(ValueError):
            stager.admit_owned(project.blob_store, **values)
