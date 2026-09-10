from __future__ import annotations

import hashlib
import io
from contextlib import closing
from pathlib import Path

import pytest

from frisket.actions.types import PdfDocument, PdfPage, StagedFile, TableError
from frisket.engine.executor import import_blob_stage
from frisket.engine.executor.import_blob_stage import AdmittedImportBlobStager
from frisket.engine.store import Project, op_log
from frisket.engine.store.blob_backend import BlobIntegrityError
from frisket.engine.store.import_blobs import publish_import_blobs
from frisket.engine.store.media_blobs import MediaBlobStore


@pytest.fixture(autouse=True)
def probe_private_copy(monkeypatch):
    def probe(path, *, filename, mime, digest):
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
        return {"kind": "file", "size_bytes": Path(path).stat().st_size}

    monkeypatch.setattr(import_blob_stage, "probe_for_ingest", probe)


def _stage(stager, data=b"file bytes", *, role=None):
    return stager.stage(
        io.BytesIO(data), filename="evidence.pdf", mime="application/pdf", role=role
    )


def _sheet(project, rows=2):
    sheet = project.add_sheet("Evidence")
    columns = {"Renamed": project.add_column(sheet, "Renamed", type="file")}
    ids = project.add_rows(sheet, [{"Renamed": None} for _ in range(rows)], columns)
    return sheet, columns, ids


def _publish(project, plan, sheet, columns):
    op_id = op_log.append_op(project, "import.pdf", spec={}, commit=False)
    return publish_import_blobs(
        project,
        plan,
        sheet_id=sheet,
        column_ids=columns,
        op_id=op_id,
        receipt_id="receipt-import",
    )


def test_acquired_url_stage_preserves_host_provenance_and_probe(tmp_path):
    with closing(Project.create(tmp_path / "url-stage.frisket")) as project:
        sheet, columns, row_ids = _sheet(project, rows=1)
        with AdmittedImportBlobStager() as stager:
            handle = stager.stage_acquired_url(
                io.BytesIO(b"downloaded bytes"),
                filename="episode.mp3",
                mime="audio/mpeg",
                source_url="https://example.com/episode",
                provider="youtube",
                acquisition={"title": "Example episode", "duration_seconds": 42.5},
            )
            digest = stager.lower(handle)["blob"]
            project.db.execute("BEGIN IMMEDIATE")
            refs = _publish(
                project,
                stager.publication_plan([(row_ids[0], "Renamed", handle)]),
                sheet,
                columns,
            )
            project.db.commit()
        assert refs[0]["source_url"] == "https://example.com/episode"
        assert refs[0]["provider"] == "youtube"
        assert (
            project.db.execute("SELECT source_url FROM blobs").fetchone()[0]
            == refs[0]["source_url"]
        )
        blobs = MediaBlobStore(project)
        assert blobs.acquisition_metadata(digest) == {
            "title": "Example episode",
            "duration_seconds": 42.5,
        }
        assert blobs.probe_metadata(digest)["size_bytes"] == len(b"downloaded bytes")


def test_stage_bounds_reads_and_keeps_borrowed_source_open():
    class Bounded(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024
            return super().read(size)

    source = Bounded(b"x" * (2 * 1024 * 1024 + 17))
    with AdmittedImportBlobStager() as stager:
        handle = stager.stage(
            source, filename="data.bin", mime="application/octet-stream"
        )
        assert handle.size == 2 * 1024 * 1024 + 17
        assert not source.closed
        with stager.open_binary(handle) as reader:
            assert reader.read(2) == b"xx"
            reader.seek(0)
            assert reader.tell() == 0
            with pytest.raises(io.UnsupportedOperation):
                reader.write(b"changed")
            with pytest.raises(io.UnsupportedOperation):
                reader.truncate(0)
            reader.close()
        with stager.open_binary(handle) as reader:
            assert reader.read(2) == b"xx"
        path = stager.publication_plan([(0, "media", handle)]).blobs[0].path
        assert path.exists()
    assert not path.exists()
    assert not source.closed


def test_cancelled_staging_stops_between_chunks_and_removes_partial_file():
    source = io.BytesIO(b"x" * (3 * 1024 * 1024))
    with AdmittedImportBlobStager(cancelled=lambda: source.tell() > 0) as stager:
        with pytest.raises(TableError, match="cancelled"):
            stager.stage(source, filename="large.bin", mime="application/octet-stream")
        assert source.tell() == 1024 * 1024
        assert not source.closed
        assert stager.publication_plan([]).blobs == ()
        # Reuse the cancelled attempt's occurrence path: partial bytes must be gone.
        source.seek(0)
        with pytest.raises(TableError, match="cancelled"):
            stager.stage(source, filename="large.bin", mime="application/octet-stream")
        assert source.tell() == 1024 * 1024


def test_cancelled_staging_does_not_read_source():
    source = io.BytesIO(b"untouched")
    with AdmittedImportBlobStager(cancelled=lambda: True) as stager:
        with pytest.raises(TableError, match="cancelled"):
            stager.stage(source, filename="large.bin", mime="application/octet-stream")
        assert source.tell() == 0
        assert not source.closed
        assert stager.publication_plan([]).blobs == ()


def test_tokens_are_invocation_owned_and_pages_require_document_role():
    with AdmittedImportBlobStager() as first, AdmittedImportBlobStager() as second:
        original = _stage(first, role=PdfDocument())
        with pytest.raises(ValueError, match="not admitted"):
            second.lower(original)
        with pytest.raises(ValueError, match="not admitted"):
            first.lower(StagedFile(size=original.size))
        with pytest.raises(ValueError, match="not admitted"):
            _stage(second, role=PdfPage(document=original, page=1))
        ordinary = _stage(first)
        with pytest.raises(ValueError, match="admitted document"):
            _stage(first, role=PdfPage(document=ordinary, page=1))
    with pytest.raises(ValueError, match="closed"):
        first.lower(original)


def test_partial_read_failure_cleans_stage_without_closing_source():
    class Broken(io.BytesIO):
        def read(self, size=-1):
            if self.tell():
                raise RuntimeError("late read failure")
            return super().read(size)

    stream = Broken(b"partial")
    with AdmittedImportBlobStager() as stager:
        with pytest.raises(RuntimeError, match="late read"):
            stager.stage(stream, filename="bad.bin", mime="application/octet-stream")
        assert stager.publication_plan([]).blobs == ()
        # Retrying uses the same private slot: a partial file must not survive.
        good = _stage(stager)
        assert good.size == len(b"file bytes")
    assert not stream.closed


def test_duplicate_bytes_keep_distinct_occurrences_and_actual_renamed_cells(tmp_path):
    with closing(Project.create(tmp_path / "occurrences.frisket")) as project:
        sheet, columns, row_ids = _sheet(project)
        with AdmittedImportBlobStager() as stager:
            first, second = _stage(stager), _stage(stager)
            assert first is not second
            assert stager.lower(first) == stager.lower(second)
            plan = stager.publication_plan(
                [
                    (1, "Renamed", second),
                    (0, "Renamed", first),
                    (1, "Renamed", first),
                ]
            ).bind_row_ordinals(row_ids)
            project.db.execute("BEGIN IMMEDIATE")
            published = _publish(project, plan, sheet, columns)
            project.db.commit()
            assert [ref["row_id"] for ref in published] == [
                row_ids[1],
                row_ids[0],
                row_ids[1],
            ]
            assert all(ref["column_id"] == columns["Renamed"] for ref in published)
            assert [ref["row_index"] for ref in published] == [2, 1, 2]
            assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1


def test_original_pdf_root_survives_without_images_or_receipt(tmp_path):
    with closing(Project.create(tmp_path / "document.frisket")) as project:
        sheet, columns, _ = _sheet(project, rows=0)
        with AdmittedImportBlobStager() as stager:
            document = _stage(stager, role=PdfDocument())
            digest = stager.lower(document)["blob"]
            project.db.execute("BEGIN IMMEDIATE")
            published = _publish(project, stager.publication_plan([]), sheet, columns)
            project.db.commit()
            assert published[0]["kind"] == "imported_pdf_document"
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
        assert (
            project.db.execute("SELECT blob_hash FROM source_artifacts").fetchone()[0]
            == digest
        )
        assert digest not in project.gc_blobs()["hashes"]


def test_pdf_pages_bind_distinct_document_handles_with_identical_bytes(tmp_path):
    with closing(Project.create(tmp_path / "pages.frisket")) as project:
        sheet, columns, row_ids = _sheet(project)
        with AdmittedImportBlobStager() as stager:
            documents = [_stage(stager, role=PdfDocument()) for _ in range(2)]
            pages = [
                _stage(stager, b"same PNG", role=PdfPage(document=doc, page=index + 1))
                for index, doc in enumerate(documents)
            ]
            plan = stager.publication_plan(
                [(row_ids[1], "Renamed", pages[0]), (row_ids[0], "Renamed", pages[1])]
            )
            project.db.execute("BEGIN IMMEDIATE")
            published = _publish(project, plan, sheet, columns)
            project.db.commit()
            docs = [ref for ref in published if ref["kind"] == "imported_pdf_document"]
            images = [
                ref for ref in published if ref["kind"] == "imported_pdf_page_image"
            ]
            assert docs[0]["artifact_id"] != docs[1]["artifact_id"]
            assert [ref["artifact_id"] for ref in images] == [
                ref["artifact_id"] for ref in docs
            ]
            assert [ref["row_id"] for ref in images] == [row_ids[1], row_ids[0]]
            assert (
                project.db.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0]
                == 2
            )


def test_stage_mutation_refuses_promotion_and_caller_rolls_back(tmp_path):
    with closing(Project.create(tmp_path / "mutation.frisket")) as project:
        sheet, columns, row_ids = _sheet(project)
        before_ops = project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]
        with AdmittedImportBlobStager() as stager:
            handle = _stage(stager)
            plan = stager.publication_plan([(row_ids[0], "Renamed", handle)])
            plan.blobs[0].path.write_bytes(b"changed bytes")
            project.db.execute("BEGIN IMMEDIATE")
            with pytest.raises(BlobIntegrityError, match="changed after staging"):
                _publish(project, plan, sheet, columns)
            project.db.rollback()
            assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0
            assert (
                project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0]
                == before_ops
            )


def test_publication_rollback_keeps_canonical_bytes_but_no_evidence_metadata(tmp_path):
    with closing(Project.create(tmp_path / "rollback.frisket")) as project:
        sheet, columns, _ = _sheet(project, rows=0)
        with AdmittedImportBlobStager() as stager:
            handle = _stage(stager, role=PdfDocument())
            digest = stager.lower(handle)["blob"]
            project.db.execute("BEGIN IMMEDIATE")
            _publish(project, stager.publication_plan([]), sheet, columns)
            project.db.rollback()  # A later receipt/write failure belongs to caller.
            assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0
            assert (
                project.db.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[
                    0
                ]
                == 0
            )
            with project.blob_store.materialize(digest) as path:
                assert path.read_bytes() == b"file bytes"


def test_publication_requires_transaction_and_wrapper_pins_digest(tmp_path):
    with closing(Project.create(tmp_path / "pin.frisket")) as project:
        sheet, columns, _ = _sheet(project, rows=0)
        with AdmittedImportBlobStager() as stager:
            handle = _stage(stager, role=PdfDocument())
            plan = stager.publication_plan([])
            with pytest.raises(ValueError, match="transaction"):
                publish_import_blobs(
                    project,
                    plan,
                    sheet_id=sheet,
                    column_ids=columns,
                    op_id=1,
                    receipt_id="none",
                )
            with pytest.raises(BlobIntegrityError):
                project.add_blob_from_path(plan.blobs[0].path, expected_digest="0" * 64)
            assert (
                project.add_blob_from_path(
                    plan.blobs[0].path, expected_digest=stager.lower(handle)["blob"]
                )
                == stager.lower(handle)["blob"]
            )


def test_backend_rechecks_stage_bytes_after_project_prehash(tmp_path, monkeypatch):
    with closing(Project.create(tmp_path / "race.frisket")) as project:
        sheet, columns, _ = _sheet(project, rows=0)
        with AdmittedImportBlobStager() as stager:
            _stage(stager, role=PdfDocument())
            plan = stager.publication_plan([])
            put_path = project.blob_store.put_path

            def changed(path, *, expected_digest):
                Path(path).write_bytes(b"changed during promotion")
                return put_path(path, expected_digest=expected_digest)

            monkeypatch.setattr(project.blob_store, "put_path", changed)
            project.db.execute("BEGIN IMMEDIATE")
            with pytest.raises(BlobIntegrityError):
                _publish(project, plan, sheet, columns)
            project.db.rollback()
            assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0


def test_final_reader_cleanup_error_does_not_mask_committed_publication(
    tmp_path, monkeypatch
):
    with closing(Project.create(tmp_path / "cleanup.frisket")) as project:
        sheet, columns, _ = _sheet(project, rows=0)
        with AdmittedImportBlobStager() as stager:
            document = _stage(stager, role=PdfDocument())
            digest = stager.lower(document)["blob"]
            project.db.execute("BEGIN IMMEDIATE")
            _publish(project, stager.publication_plan([]), sheet, columns)
            project.db.commit()

            def failed_close():
                raise OSError("reader cleanup failed")

            monkeypatch.setattr(stager._readers, "close", failed_close)
        assert project.db.execute("SELECT hash FROM blobs").fetchone()[0] == digest
