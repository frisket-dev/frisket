from __future__ import annotations

import io
import asyncio
import os
import shutil
from itertools import islice
from pathlib import Path
from types import SimpleNamespace

import pytest

from frisket.actions.types import PdfDocument, TableError
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor import pdf_page_read, run_action_spec
from frisket.engine.executor.action_inventory import ExecutorDeps, ImportWorkloadLimits
from frisket.engine.executor.import_blob_stage import AdmittedImportBlobStager
from frisket.engine.executor.pdf_page_read import AdmittedPdfPageRenderer
from frisket.engine.executor.table_action import prepare_table_producer
from frisket.engine.store import Project
from frisket.engine.sandbox.shim import SandboxResult, SandboxTeardownError
from frisket.engine.sandbox import fence


def _sandbox(monkeypatch, factory):
    async def invoke(command, **kwargs):
        return factory(command, **kwargs)

    monkeypatch.setattr(pdf_page_read, "run_sandboxed", invoke)


def _document(blobs):
    return blobs.stage(
        io.BytesIO(b"original PDF"),
        filename="document.pdf",
        mime="application/pdf",
        role=PdfDocument(),
    )


@pytest.mark.parametrize(
    "limit,expected", [(None, {1, 2, 10}), (2, {1, 2}), (0, set())]
)
def test_renderer_uses_one_admitted_source_and_numeric_page_roles(
    monkeypatch, limit, expected
):
    calls = []
    monkeypatch.setattr(pdf_page_read.shutil, "which", lambda _: "/tools/pdftoppm")
    with AdmittedImportBlobStager() as blobs:
        document = _document(blobs)
        admitted = blobs._admitted(document)

        def sandbox(command, **kwargs):
            calls.append(command)
            assert Path(command[-2]) == admitted.path  # No second full-PDF copy.
            policy = kwargs["policy"]
            scratch = Path(command[-1]).parent
            assert kwargs["scratch_dir"] == scratch
            assert policy.memory_mb == 2048
            assert policy.cpu_seconds == policy.wall_seconds == 30
            assert policy.allow_network is False
            assert policy.env_passthrough == ["PATH"]
            assert policy.confine.read == (str(admitted.path), "/etc/fonts")
            assert policy.confine.write == (str(scratch),)
            assert policy.confine.exec_binary == command[0]
            assert not kwargs["should_cancel"]()
            for page in (10, 2, 1):
                Path(f"{command[-1]}-{page}.png").write_bytes(b"same page bytes")
            return SandboxResult(0, "", "")

        _sandbox(monkeypatch, sandbox)
        renderer = AdmittedPdfPageRenderer(blobs, page_limit=limit)
        images = renderer.render(document, dpi=175)
        assert set(images) == expected
        assert list(images) == sorted(expected)
        assert len(calls) == (0 if limit == 0 else 1)
        if calls:
            assert calls[0][1:5] == ["-q", "-png", "-r", "175"]
            assert calls[0][5:-2] == (
                [] if limit is None else ["-f", "1", "-l", str(limit)]
            )
            assert not Path(calls[0][-1]).parent.exists()
        for page, image in images.items():
            artifact = blobs._admitted(image)
            assert artifact.page == page
            assert artifact.document_id == admitted.occurrence_id
            assert artifact.filename == f"document-p{page:04d}.png"
        renderer.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_renderer_preserves_sandbox_timeout_or_cancellation(monkeypatch, cancel):
    calls = []
    monkeypatch.setattr(pdf_page_read.shutil, "which", lambda _: "/tools/pdftoppm")

    def sandbox(command, **kwargs):
        calls.append(command)
        return SandboxResult(-1, "", "", cancelled=cancel, timed_out=not cancel)

    _sandbox(monkeypatch, sandbox)
    with AdmittedImportBlobStager() as blobs:
        document = _document(blobs)
        renderer = AdmittedPdfPageRenderer(blobs)
        if cancel:
            with pytest.raises(TableError) as caught:
                renderer.render(document, dpi=150)
            assert caught.value.code == "action_cancelled"
        else:
            assert renderer.render(document, dpi=150) == {}
        assert len(calls) == 1
        assert not Path(calls[0][-1]).parent.exists()
        renderer.close()


def test_renderer_absence_and_foreign_document_do_not_spawn(monkeypatch):
    monkeypatch.setattr(pdf_page_read.shutil, "which", lambda _: None)
    _sandbox(monkeypatch, lambda *a, **k: pytest.fail("no spawn"))
    with AdmittedImportBlobStager() as blobs, AdmittedImportBlobStager() as foreign:
        renderer = AdmittedPdfPageRenderer(blobs)
        assert renderer.render(_document(blobs), dpi=150) == {}
        with pytest.raises(ValueError, match="not admitted"):
            renderer.render(_document(foreign), dpi=150)
        attachment = blobs.stage(
            io.BytesIO(b"PDF"), filename="file.pdf", mime="application/pdf"
        )
        with pytest.raises(ValueError, match="PDF document"):
            renderer.render(attachment, dpi=150)
        renderer.close()
        with pytest.raises(TableError, match="cancelled"):
            renderer.render(_document(blobs), dpi=150)


@pytest.mark.parametrize(
    "failure",
    [
        SandboxTeardownError("not reaped"),
        fence.SandboxEnforcementUnavailable("fence refused"),
        asyncio.CancelledError(),
    ],
)
def test_renderer_does_not_turn_failed_teardown_or_outer_cancel_into_text_fallback(
    monkeypatch, failure
):
    calls = []
    monkeypatch.setattr(pdf_page_read.shutil, "which", lambda _: "/tools/pdftoppm")

    def fail(command, **kwargs):
        calls.append(command)
        raise failure

    _sandbox(monkeypatch, fail)
    with AdmittedImportBlobStager() as blobs:
        with pytest.raises(type(failure)):
            AdmittedPdfPageRenderer(blobs).render(_document(blobs), dpi=150)
    assert len(calls) == 1  # Never retry with an unconfined process.


def test_renderer_unavailable_process_falls_back_and_cleans(monkeypatch):
    directories = []
    monkeypatch.setattr(pdf_page_read.shutil, "which", lambda _: "/tools/pdftoppm")

    def missing(command, **kwargs):
        directories.append(Path(command[-1]).parent)
        raise OSError("executable unavailable")

    _sandbox(monkeypatch, missing)
    with AdmittedImportBlobStager() as blobs:
        assert AdmittedPdfPageRenderer(blobs).render(_document(blobs), dpi=150) == {}
    assert len(directories) == 1 and not directories[0].exists()


@pytest.mark.parametrize("entry_kind", ["symlink", "fifo", "directory"])
def test_renderer_skips_nonregular_child_output(tmp_path, monkeypatch, entry_kind):
    if entry_kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("platform has no FIFOs")
    canary = tmp_path / "outside-secret"
    canary.write_bytes(b"never admit this canary")
    monkeypatch.setattr(pdf_page_read.shutil, "which", lambda _: "/tools/pdftoppm")
    ordinary_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if entry_kind == "fifo" and path.name == "page-1.png":
            pytest.fail("blocking Path.open on a child-created FIFO")
        return ordinary_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    def render(command, **kwargs):
        malicious = Path(f"{command[-1]}-1.png")
        if entry_kind == "symlink":
            malicious.symlink_to(canary)
        elif entry_kind == "fifo":
            os.mkfifo(malicious)
        else:
            malicious.mkdir()
        Path(f"{command[-1]}-2.png").write_bytes(b"ordinary image")
        return SandboxResult(0, "", "")

    _sandbox(monkeypatch, render)
    with AdmittedImportBlobStager() as blobs:
        images = AdmittedPdfPageRenderer(blobs).render(_document(blobs), dpi=150)
        assert list(images) == [2]
        assert len(blobs._manifest) == 2  # Original document and safe page only.
        assert blobs.describe(images[2]).path.read_bytes() == b"ordinary image"


@pytest.mark.parametrize("replacement", ["symlink", "fifo", "regular"])
def test_renderer_rejects_output_replaced_after_lstat(
    tmp_path, monkeypatch, replacement
):
    if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_NONBLOCK", "mkfifo")):
        pytest.skip("requires POSIX nonblocking/no-follow file admission")
    canary = tmp_path / "outside-secret"
    canary.write_bytes(b"never admit this canary")
    monkeypatch.setattr(pdf_page_read.shutil, "which", lambda _: "/tools/pdftoppm")
    ordinary_open = os.open
    replaced = []

    def replace_at_open(path, flags, *args, **kwargs):
        if Path(path).name == "page-1.png":
            assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK
            # Retain the old inode so a new regular file cannot reuse it.
            Path(path).rename(Path(path).with_suffix(".original"))
            if replacement == "symlink":
                Path(path).symlink_to(canary)
            elif replacement == "fifo":
                os.mkfifo(path)
            else:
                Path(path).write_bytes(b"replacement image")
            replaced.append(path)
        return ordinary_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_at_open)

    def render(command, **kwargs):
        Path(f"{command[-1]}-1.png").write_bytes(b"ordinary image")
        return SandboxResult(0, "", "")

    _sandbox(monkeypatch, render)
    with AdmittedImportBlobStager() as blobs:
        images = AdmittedPdfPageRenderer(blobs).render(_document(blobs), dpi=150)
        assert images == {}
        assert len(blobs._manifest) == 1
        assert len(replaced) == 1


@pytest.mark.parametrize("preview", [False, True])
def test_pdf_teardown_failure_escapes_host_without_publication(
    tmp_path, monkeypatch, preview
):
    from frisket.engine.executor.table_preview import preview_table
    from tests.engine.test_import_pdf_executor import _action, _counts, _pdf_bytes

    source = tmp_path / "document.pdf"
    source.write_bytes(_pdf_bytes())
    monkeypatch.setattr(pdf_page_read.shutil, "which", lambda _: "/tools/pdftoppm")

    def fail(command, **kwargs):
        raise SandboxTeardownError("unreaped child")

    _sandbox(monkeypatch, fail)
    project = Project.create(tmp_path / "teardown.frisket", name="Teardown")
    try:
        before = _counts(project)
        with pytest.raises(SandboxTeardownError, match="unreaped child"):
            if preview:
                preview_table(
                    project,
                    "teardown",
                    typed_action_for_request(_action(source)),
                    deps=ExecutorDeps(),
                    progress=lambda *_: None,
                    cancelled=lambda: False,
                )
            else:
                run_action_spec(project, _action(source), project_id="teardown")
        assert _counts(project) == before
    finally:
        project.close()


def test_renderer_close_reaches_the_existing_sandbox_cancellation_signal(monkeypatch):
    monkeypatch.setattr(pdf_page_read.shutil, "which", lambda _: "/tools/pdftoppm")
    with AdmittedImportBlobStager() as blobs:
        renderer = AdmittedPdfPageRenderer(blobs)

        def stop(command, **kwargs):
            assert not kwargs["should_cancel"]()
            renderer.close()
            assert kwargs["should_cancel"]()
            return SandboxResult(-1, "", "", cancelled=True)

        _sandbox(monkeypatch, stop)
        with pytest.raises(TableError, match="cancelled"):
            renderer.render(_document(blobs), dpi=150)


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler not installed")
def test_real_renderer_preserves_nonembedded_font_pixels(tmp_path):
    # The inherited sandbox owns a real child process here. The comparison
    # fixture specifically needs /etc/fonts, not just embedded font bytes.
    from tests.engine.test_sandbox_media_fence import _nonembedded_font_pdf, _rasterize

    reason = fence.kernel_can_confine()
    if reason is not None:
        pytest.skip(f"this kernel cannot install the fence: {reason}")
    source = tmp_path / "font.pdf"
    source.write_bytes(_nonembedded_font_pdf())
    plain = tmp_path / "plain"
    plain.mkdir()
    _rasterize(source, plain, confined=False, dpi=72)
    with AdmittedImportBlobStager() as blobs:
        document = blobs.stage(
            io.BytesIO(source.read_bytes()),
            filename="font.pdf",
            mime="application/pdf",
            role=PdfDocument(),
        )
        renderer = AdmittedPdfPageRenderer(blobs)
        try:
            images = renderer.render(document, dpi=72)
            assert list(images) == [1]
            assert (
                blobs.describe(images[1]).path.read_bytes()
                == (plain / "page-1.png").read_bytes()
            )
        finally:
            renderer.close()


@pytest.mark.parametrize("maximum", [0, 1, 2, 3])
def test_full_run_bounds_rasterization_but_does_not_truncate_overflow(
    tmp_path, monkeypatch, maximum
):
    from tests.engine.test_import_pdf_executor import _action, _pdf_bytes

    source = tmp_path / "two-pages.pdf"
    source.write_bytes(_pdf_bytes())
    calls = []
    monkeypatch.setattr(pdf_page_read.shutil, "which", lambda _: "/tools/pdftoppm")

    def render(command, **kwargs):
        calls.append(command)
        return SandboxResult(0, "", "")

    _sandbox(monkeypatch, render)
    project = Project.create(tmp_path / "limited.frisket", name="Limited")
    try:
        result = run_action_spec(
            project,
            _action(source),
            project_id="limited",
            deps=ExecutorDeps(
                import_workload_limits=ImportWorkloadLimits(max_rows=maximum)
            ),
        )
        assert len(calls) == (0 if maximum == 0 else 1)
        if calls:
            assert calls[0][5:-2] == ["-f", "1", "-l", str(maximum)]
        if maximum < 2:
            assert result.status == "failed"
            assert result.errors[0].code == "import_workload_limit_exceeded"
            assert result.errors[0].details == {
                "row_count": maximum + 1,
                "max_rows": maximum,
            }
            for table in ("sheets", "rows", "blobs"):
                assert (
                    project.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    == 0
                )
        else:
            assert result.status == "completed", result.errors
            assert project.db.execute("SELECT count(*) FROM rows").fetchone()[0] == 2
    finally:
        project.close()


@pytest.mark.parametrize("limit", [-1, True, 1.5])
def test_renderer_refuses_invalid_host_budget(limit):
    with AdmittedImportBlobStager() as blobs:
        with pytest.raises(ValueError, match="nonnegative integer"):
            AdmittedPdfPageRenderer(blobs, page_limit=limit)


@pytest.mark.parametrize(
    "schema_only,limit,maximum",
    [
        (True, 2, None),
        (False, 2, None),
        (False, None, None),
        (False, 2, 5),
        (False, 5, 2),
    ],
)
def test_prepared_pdf_limits_rendering_and_lazy_text_without_changing_params(
    tmp_path, monkeypatch, schema_only, limit, maximum
):
    pypdf = pytest.importorskip("pypdf")
    source = tmp_path / "document.pdf"
    source.write_bytes(b"original PDF")
    extracted, parsed, calls = [], [], []
    page_limit = min(
        (value for value in (limit, maximum) if value is not None), default=None
    )

    def pages():
        for page in range(1, 13):
            parsed.append(page)
            yield SimpleNamespace(
                extract_text=lambda page=page: extracted.append(page) or str(page)
            )

    monkeypatch.setattr(
        pypdf, "PdfReader", lambda stream: SimpleNamespace(pages=pages())
    )
    monkeypatch.setattr(pdf_page_read.shutil, "which", lambda _: "/tools/pdftoppm")

    def sandbox(command, **kwargs):
        calls.append(command)
        for page in range(1, (page_limit or 12) + 1):
            Path(f"{command[-1]}-{page}.png").write_bytes(b"page image")
        return SandboxResult(0, "", "")

    _sandbox(monkeypatch, sandbox)
    bound = typed_action_for_request(
        {
            "action_id": "import.pdf",
            "scope": {"kind": "project"},
            "sheet_name": "Pages",
            "idempotency_key": "bounded-pdf",
            "params": {"source": {"kind": "file", "path": str(source)}},
        }
    )
    project = Project.create(tmp_path / "preview.frisket", name="Preview")
    try:
        with AdmittedImportBlobStager() as blobs:
            with prepare_table_producer(
                project,
                bound,
                blob_stager=blobs,
                schema_only=schema_only,
                row_limit=limit,
                deps=ExecutorDeps(
                    import_workload_limits=ImportWorkloadLimits(max_rows=maximum)
                ),
            ) as prepared:
                rows = (
                    list(islice(prepared.rows, page_limit))
                    if page_limit is not None
                    else list(prepared.rows)
                )
                assert len(rows) == (0 if schema_only else page_limit or 12)
            expected = [] if schema_only else list(range(1, (page_limit or 12) + 1))
            assert extracted == parsed == expected
            assert len(blobs._manifest) == (0 if schema_only else 1 + len(expected))
            assert len(calls) == (0 if schema_only else 1)
            if calls:
                assert calls[0][5:-2] == (
                    [] if page_limit is None else ["-f", "1", "-l", str(page_limit)]
                )
            assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
    finally:
        project.close()


def test_preparation_cancellation_closes_pdf_stream_and_publishes_nothing(
    tmp_path, monkeypatch
):
    pypdf = pytest.importorskip("pypdf")
    source = tmp_path / "document.pdf"
    source.write_bytes(b"original PDF")
    streams = []

    def parse(stream):
        streams.append(stream)
        return SimpleNamespace(
            pages=[SimpleNamespace(extract_text=lambda: pytest.fail("cancelled text"))]
        )

    monkeypatch.setattr(pypdf, "PdfReader", parse)
    _sandbox(monkeypatch, lambda *a, **k: pytest.fail("cancelled render"))
    bound = typed_action_for_request(
        {
            "action_id": "import.pdf",
            "scope": {"kind": "project"},
            "sheet_name": "Pages",
            "idempotency_key": "cancelled-pdf",
            "params": {"source": {"kind": "file", "path": str(source)}},
        }
    )
    project = Project.create(tmp_path / "cancelled.frisket", name="Cancelled")
    try:
        with AdmittedImportBlobStager() as blobs:
            with pytest.raises(TableError) as caught:
                with prepare_table_producer(
                    project, bound, blob_stager=blobs, cancelled=lambda: True
                ):
                    pytest.fail("cancelled preparation returned rows")
            assert caught.value.code == "action_cancelled"
            assert streams == []
            assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
            assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0
    finally:
        project.close()
