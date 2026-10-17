from __future__ import annotations

import asyncio
import errno
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from frisket.ai.models.artifact_manifest import lookup
from frisket.engine._workers.indra_ocr_model import (
    MODEL_DOWNLOAD_SIZE,
    MODEL_FILES,
    MODEL_LICENSE,
    MODEL_LICENSE_URL,
    MODEL_REF,
    MODEL_REPO,
    MODEL_REVISION,
)
from frisket.engine._workers.indra_ocr_worker import _original_box, _page_result
from frisket.engine._workers import indra_ocr_worker, pdf_render_worker
from frisket.engine.sandbox.shim import SandboxResult
from frisket.engine.sandbox.shim import SandboxTeardownError
from frisket.ops import ocr_engines_local


def test_indra_pdf_worker_loads_once_and_releases_each_page(
    tmp_path, monkeypatch, capsys
):
    import sys

    rendered, loaded, closed = [], [], []

    class Document:
        def __len__(self):
            return 500

        def close(self):
            closed.append(True)

    def render(doc, number, output, *, dpi):
        assert dpi == 200
        assert not list(tmp_path.glob("*.png"))
        output.write_bytes(b"page fixture")
        rendered.append(number)
        if number == 2:
            raise RuntimeError("private renderer failure")
        return (100, 200)

    class Converter:
        def convert(self, path):
            assert Path(path).exists()
            number = rendered[-1]
            if number == 4:
                raise RuntimeError("private model failure")
            yield (
                0,
                SimpleNamespace(
                    source="ocr",
                    rotation=0,
                    lines=[]
                    if number == 3
                    else [
                        SimpleNamespace(
                            text=f"Page {number}",
                            box=[10, 20, 30, 40],
                            conf=0.9,
                        )
                    ],
                ),
            )

    converter = Converter()

    def load(**kwargs):
        loaded.append(kwargs)
        return converter

    monkeypatch.setitem(sys.modules, "indra_ocr", SimpleNamespace(load=load))
    monkeypatch.setitem(
        sys.modules,
        "cv2",
        SimpleNamespace(
            IMREAD_GRAYSCALE=0,
            imread=lambda *args: SimpleNamespace(shape=(200, 100)),
        ),
    )
    monkeypatch.setitem(
        sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=lambda path: Document())
    )
    monkeypatch.setattr(pdf_render_worker, "render_page", render)
    output = tmp_path / "result.json"
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "out": str(output),
                    "pdf": str(tmp_path / "source.pdf"),
                    "dpi": 200,
                    "model_dir": "/offline/model",
                    "threads": 1,
                }
            )
        ),
    )
    indra_ocr_worker.main()
    pages = json.loads(output.read_text())["pages"]
    assert len(loaded) == len(closed) == 1
    assert rendered == list(range(1, 501))
    assert len(pages) == 500 and pages[-1]["text"] == "Page 500"
    assert pages[1]["error"]["stage"] == "rendering"
    assert pages[3]["error"]["stage"] == "recognition"
    assert pages[2]["text"] == "" and "error" not in pages[2]
    assert pages[-1]["width"] == 100 and pages[-1]["height"] == 200
    assert not list(tmp_path.glob("*.png"))
    progress = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["completed"] for event in progress] == list(range(1, 501))
    assert all(event["total"] == 500 for event in progress)


@pytest.mark.parametrize("failure_stage", ["rendering", "recognition"])
@pytest.mark.parametrize("failure", ["storage", "teardown", "persistent"])
def test_indra_pdf_stops_fatal_or_repeated_failures_and_releases_raster(
    tmp_path, monkeypatch, failure_stage, failure
):
    import sys

    observed, closed = [], []

    class Document:
        def __len__(self):
            return 30

        def close(self):
            closed.append(True)

    def fail(number):
        if number == 1:
            return
        if failure == "storage":
            raise OSError(errno.ENOSPC, "private storage path")
        if failure == "teardown":
            raise SandboxTeardownError("unverified process tree")
        raise RuntimeError("private page failure")

    def render(doc, number, output, *, dpi):
        observed.append(number)
        output.write_bytes(b"page fixture")
        if failure_stage == "rendering":
            fail(number)
        return 12, 8

    def recognize(*args, **kwargs):
        if failure_stage == "recognition":
            fail(observed[-1])
        return {"text": "", "blocks": []}

    monkeypatch.setitem(
        sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=lambda path: Document())
    )
    monkeypatch.setattr(pdf_render_worker, "render_page", render)
    monkeypatch.setattr(indra_ocr_worker, "_recognize_page", recognize)
    expected = (
        OSError
        if failure == "storage"
        else SandboxTeardownError
        if failure == "teardown"
        else RuntimeError
    )
    with pytest.raises(expected):
        indra_ocr_worker._pdf_pages(
            None,
            {"pdf": "fixture.pdf", "out": str(tmp_path / "result.json"), "dpi": 200},
            lambda *args: None,
        )
    assert observed == list(range(1, 7 if failure == "persistent" else 3))
    assert closed == [True] and not list(tmp_path.glob("*.png"))


@pytest.mark.parametrize(
    ("pdf_mode", "failure_stage"),
    [
        (True, "rendering"),
        (True, "result_write"),
        (True, "teardown"),
        (False, "result_write"),
    ],
)
def test_indra_fatal_failure_crosses_main_and_host_without_writable_result(
    tmp_path, monkeypatch, pdf_mode, failure_stage
):
    import sys
    from contextlib import redirect_stdout
    from frisket.engine._workers import indra_ocr_artifacts
    from frisket.ops.base import RecipeInvocationHalt
    from frisket.runtime import indra_ocr_install

    model_dir = tmp_path / "cache" / "snapshots" / MODEL_REVISION
    model_dir.mkdir(parents=True)
    monkeypatch.setattr(indra_ocr_artifacts, "cached_indra_ocr_path", lambda: model_dir)
    monkeypatch.setattr(
        indra_ocr_install, "runtime_python", lambda: Path(sys.executable)
    )
    monkeypatch.setattr(ocr_engines_local, "indra_ocr_available", lambda: (True, None))
    loaded, rendered, closed, signals = [], [], [], []

    def load(**kwargs):
        loaded.append(True)
        return object()

    class Document:
        def __len__(self):
            return 2

        def close(self):
            closed.append(True)

    def render(doc, number, output, *, dpi):
        rendered.append(number)
        output.write_bytes(b"page fixture")
        if number == 2 and failure_stage == "rendering":
            raise OSError(errno.ENOSPC, "private storage path")
        if number == 2 and failure_stage == "teardown":
            raise SandboxTeardownError("unverified process tree")
        return 12, 8

    def write(*args, **kwargs):
        raise OSError(errno.ENOSPC, "private result path")

    monkeypatch.setitem(sys.modules, "indra_ocr", SimpleNamespace(load=load))
    monkeypatch.setitem(
        sys.modules,
        "cv2",
        SimpleNamespace(
            IMREAD_GRAYSCALE=0, imread=lambda *args: SimpleNamespace(shape=(8, 12))
        ),
    )
    monkeypatch.setitem(
        sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=lambda path: Document())
    )
    monkeypatch.setattr(pdf_render_worker, "render_page", render)
    monkeypatch.setattr(
        indra_ocr_worker,
        "_recognize_page",
        lambda *args, **kwargs: {"text": "", "blocks": []},
    )
    if failure_stage == "result_write":
        monkeypatch.setattr(indra_ocr_worker, "_write", write)

    async def launch(argv, **kwargs):
        monkeypatch.setattr(sys, "stdin", io.StringIO(kwargs["stdin_data"].decode()))
        stream = io.StringIO()
        with redirect_stdout(stream):
            indra_ocr_worker.main()
        wire = stream.getvalue()
        signals.extend(json.loads(line) for line in wire.splitlines())
        if pdf_mode:
            for line in wire.splitlines():
                kwargs["on_stdout_line"](line)
        return SandboxResult(returncode=0, stdout="" if pdf_mode else wire, stderr="")

    monkeypatch.setattr(
        ocr_engines_local,
        "run_sandboxed_stdout_lines" if pdf_mode else "run_sandboxed",
        launch,
    )
    with pytest.raises(RecipeInvocationHalt) as raised:
        asyncio.run(
            ocr_engines_local.ocr_indra(
                [] if pdf_mode else [tmp_path / "page.png"],
                tmp_path,
                **(
                    {"pdf": tmp_path / "source.pdf", "page_count": 2}
                    if pdf_mode
                    else {}
                ),
            )
        )
    assert raised.value.code == "local_session_failed"
    assert loaded == [True]
    assert signals[-1] == {"error": "environment_failed"}
    assert not (tmp_path / "indra-ocr-result.json").exists()
    assert not list(tmp_path.glob("*.png"))
    if pdf_mode:
        assert rendered == [1, 2] and closed == [True]


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        (0, [10.0, 20.0, 30.0, 40.0]),
        (90, [20.0, 70.0, 40.0, 90.0]),
        (180, [170.0, 60.0, 190.0, 80.0]),
        (270, [160.0, 10.0, 180.0, 30.0]),
    ],
)
def test_indra_line_boxes_map_from_upright_page_to_input(rotation, expected):
    assert (
        _original_box([10, 20, 30, 40], rotation=rotation, width=200, height=100)
        == expected
    )


def test_indra_page_projection_preserves_reading_order_score_and_geometry():
    page = SimpleNamespace(
        source="ocr",
        rotation=90,
        skew=1.7,
        lines=[
            SimpleNamespace(text=" First line ", box=[10, 20, 30, 40], conf=0.98765),
            SimpleNamespace(text="Second line", box=[35, 25, 70, 50], conf=0.75),
        ],
    )

    result = _page_result(page, width=200, height=100)

    assert result["text"] == "First line\nSecond line"
    assert result["blocks"] == [
        {
            "text": "First line",
            "bbox": [[20.0, 70.0], [40.0, 70.0], [40.0, 90.0], [20.0, 90.0]],
            "score": 0.9877,
        },
        {
            "text": "Second line",
            "bbox": [[25.0, 30.0], [50.0, 30.0], [50.0, 65.0], [25.0, 65.0]],
            "score": 0.75,
        },
    ]
    with pytest.raises(RuntimeError, match="could not read"):
        _page_result(SimpleNamespace(source="error"), width=200, height=100)


def test_indra_model_manifest_is_revision_pinned_with_exact_files():
    artifact = lookup(MODEL_REF)
    assert artifact is not None
    assert artifact.integrity == "hf_revision_pinned"
    assert artifact.license_url == MODEL_LICENSE_URL
    assert artifact.license == "LicenseRef-Datalab-Indra-OpenRAIL-M-Modified"
    assert MODEL_LICENSE_URL == (
        "https://huggingface.co/datalab-to/indra-ocr/blob/"
        "d7c839c892bfe4b8993373db64c62f154d05d70b/LICENSE"
    )
    assert MODEL_LICENSE == artifact.license
    assert artifact.approx_size_bytes == MODEL_DOWNLOAD_SIZE
    assert artifact.hf_snapshot is not None
    assert artifact.hf_snapshot.repo_id == MODEL_REPO
    assert artifact.hf_snapshot.revision == MODEL_REVISION
    assert artifact.hf_snapshot.files == MODEL_FILES


@pytest.mark.parametrize("pdf_mode", [False, True])
def test_indra_local_adapter_uses_private_runtime_offline_and_validates_pages(
    tmp_path, monkeypatch, pdf_mode
):
    from frisket.engine._workers import indra_ocr_artifacts
    from frisket.runtime import indra_ocr_install

    repo_cache = tmp_path / "models--datalab-to--indra-ocr"
    model_dir = repo_cache / "snapshots" / MODEL_REVISION
    model_dir.mkdir(parents=True)
    page = tmp_path / "page.png"
    page.touch()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    runtime = tmp_path / "private" / "bin" / "python"
    runtime.parent.mkdir(parents=True)
    runtime.touch(mode=0o755)
    monkeypatch.setattr(indra_ocr_artifacts, "indra_ocr_runtime_present", lambda: True)
    monkeypatch.setattr(indra_ocr_artifacts, "cached_indra_ocr_path", lambda: model_dir)
    monkeypatch.setattr(indra_ocr_install, "runtime_python", lambda: runtime)
    captured = {}
    expected = [{"text": "local", "blocks": []}] * (2 if pdf_mode else 1)
    progress = []

    async def run_sandboxed(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        payload = json.loads(kwargs["stdin_data"])
        Path(payload["out"]).write_text(
            json.dumps({"pages": expected}),
            encoding="utf-8",
        )
        captured["payload"] = payload
        if pdf_mode:
            kwargs["on_stdout_line"]("not a progress event")
            for number in (1, 2):
                kwargs["on_stdout_line"](
                    json.dumps(
                        {
                            "kind": "ocr_pages",
                            "completed": number,
                            "total": 2,
                        }
                    )
                )
        return SandboxResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ocr_engines_local, "run_sandboxed", run_sandboxed)
    monkeypatch.setattr(ocr_engines_local, "run_sandboxed_stdout_lines", run_sandboxed)

    result = asyncio.run(
        ocr_engines_local.ocr_indra(
            [] if pdf_mode else [page],
            scratch,
            **(
                {
                    "pdf": page,
                    "dpi": 150,
                    "page_count": 2,
                    "report_progress": lambda done, total: progress.append(
                        (done, total)
                    ),
                }
                if pdf_mode
                else {}
            ),
        )
    )

    assert result == expected
    if pdf_mode:
        assert captured["payload"]["pdf"] == str(page)
        assert captured["payload"]["dpi"] == 150
        assert "paths" not in captured["payload"]
        assert progress == [(1, 2), (2, 2)]
    else:
        assert captured["payload"]["paths"] == [str(page)]
    assert captured["argv"][0] == str(runtime)
    assert captured["argv"][3] == "indra-ocr"
    assert captured["payload"]["model_dir"] == str(model_dir)
    assert captured["policy"].allow_network is False
    assert captured["policy"].trusted_python_netwall is True
    assert str(page) in captured["policy"].confine.read
    assert str(repo_cache) in captured["policy"].confine.read
    assert captured["policy"].confine.write == (str(scratch),)
