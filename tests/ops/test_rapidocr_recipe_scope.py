from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from frisket.contracts.actions.schemas._engines import OCR_ENGINE_TABLE
from frisket.engine._workers.local_engine_lease import LocalEngineLeaseBusy
from frisket.engine._workers.rapidocr_models import (
    RapidOCRInvalidLanguage,
    rapidocr_model_requirements,
)
from frisket.engine._workers.rapidocr_session import (
    RapidOCRRowError,
    RapidOCRSessionCancelled,
    RapidOCRSessionFailure,
)
from frisket.engine.sandbox.shim import SandboxTeardownError
from frisket.ops.base import OpContext, RecipeInvocationHalt
from frisket.ops import ocr_engines as ocr_mod
from frisket.ops import ocr_engines_local as ocr_local
from frisket.ops.ocr_engines import OcrCancelled, OcrEngines, rapidocr_execution_scope
from frisket.engine.executor.ocr_read import AdmittedOcrReader
from frisket.actions.media_options import OcrOptions


class _FakeLease:
    def __init__(self) -> None:
        self.released = 0
        self.poisoned = 0

    def release(self) -> None:
        self.released += 1

    def poison(self) -> None:
        self.poisoned += 1


class _FakePool:
    instances: list["_FakePool"] = []
    close_error: BaseException | None = None
    teardown_on_close = False

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.paths: list[list[str]] = []
        self.close_calls = 0
        self.closed = False
        self.teardown_failed = False
        self.instances.append(self)

    async def ocr(self, paths: list[str]) -> list[dict[str, Any]]:
        self.paths.append(paths)
        return [{"text": "pooled", "blocks": []} for _ in paths]

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True
        self.teardown_failed = self.teardown_on_close
        if self.close_error is not None:
            raise self.close_error


def _topology(*, workers: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        effective_cpus=4,
        effective_memory_bytes=8 * 1024**3,
        workers=workers,
        onnx_intra_threads=2,
        onnx_inter_threads=1,
        opencv_threads=1,
        memory_mb=3584,
    )


@pytest.fixture(autouse=True)
def _reset_pool_state() -> None:
    _FakePool.instances = []
    _FakePool.close_error = None
    _FakePool.teardown_on_close = False


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    lease: _FakeLease,
    *,
    workers: int = 2,
) -> None:
    monkeypatch.setattr(ocr_local, "RapidOCRProcessPool", _FakePool)
    monkeypatch.setattr(
        ocr_local,
        "rapidocr_topology",
        lambda expected_rows=None: _topology(workers=workers),
    )
    monkeypatch.setattr(
        ocr_local, "_rapidocr_model_root_dir", lambda language=None: "/models"
    )
    monkeypatch.setattr(ocr_local, "acquire_local_engine_lease", lambda engine: lease)


def test_rapidocr_declaration_drives_topology_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rapidocr = next(entry for entry in OCR_ENGINE_TABLE if entry.id == "rapidocr")
    assert rapidocr.run_scoped is True
    monkeypatch.setattr(
        "frisket.engine._workers.rapidocr_session.rapidocr_topology",
        lambda expected_rows=None: _topology(workers=3),
    )

    from frisket.actions.media import OCR
    from frisket.engine.executor.map_rows_action import _TypedMapRowsProgram
    from functools import partial

    recipe = SimpleNamespace(
        max_row_concurrency=partial(
            _TypedMapRowsProgram.max_row_concurrency, SimpleNamespace(_terminal=OCR.run)
        )
    )
    assert recipe.max_row_concurrency({}) == 3
    assert recipe.max_row_concurrency({"engine": "rapidocr"}) == 3
    assert recipe.max_row_concurrency({"engine": "tesseract"}) is None
    assert recipe.max_row_concurrency({"engine": "dots.mocr"}) is None
    assert recipe.max_row_concurrency({"engine": "openai/gpt-4.1"}) is None


def test_non_rapidocr_scope_allocates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("RapidOCR resources must not be resolved")

    monkeypatch.setattr(ocr_local, "_rapidocr_model_root_dir", forbidden)
    monkeypatch.setattr(ocr_local, "acquire_local_engine_lease", forbidden)

    async def run() -> None:
        reader = AdmittedOcrReader(
            OpContext(),
            None,
            engine="tesseract",
            options=OcrOptions().normalize("tesseract"),
        )
        await reader.start(expected_rows=4)
        await reader.aclose()

    asyncio.run(run())


def test_precancelled_scope_is_resource_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("pre-cancelled scope must not resolve or admit resources")

    monkeypatch.setattr(ocr_local, "_rapidocr_model_root_dir", forbidden)
    monkeypatch.setattr(ocr_local, "rapidocr_topology", forbidden)
    monkeypatch.setattr(ocr_local, "acquire_local_engine_lease", forbidden)
    monkeypatch.setattr(ocr_local, "RapidOCRProcessPool", forbidden)

    async def run() -> None:
        async with rapidocr_execution_scope(
            expected_rows=4, language=None, cancelled=lambda: True
        ):
            pytest.fail("cancelled scope entered")

    with pytest.raises(OcrCancelled):
        asyncio.run(run())


def test_invalid_topology_fails_before_local_engine_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ocr_local, "_rapidocr_model_root_dir", lambda language=None: None
    )

    def invalid_topology(expected_rows: int | None = None) -> Any:
        raise ValueError("FRISKET_RAPIDOCR_WORKERS is invalid")

    def forbidden(engine: str) -> Any:
        raise AssertionError("invalid topology must not acquire a lease")

    monkeypatch.setattr(ocr_local, "rapidocr_topology", invalid_topology)
    monkeypatch.setattr(ocr_local, "acquire_local_engine_lease", forbidden)

    async def run() -> None:
        async with rapidocr_execution_scope(expected_rows=2, language=None):
            pass

    with pytest.raises(ValueError, match="FRISKET_RAPIDOCR_WORKERS"):
        asyncio.run(run())


def test_scope_installs_pool_once_and_resets_before_verified_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _FakeLease()
    _install_fakes(monkeypatch, lease, workers=2)
    model_root_calls = 0

    def model_root(language: str | None = None) -> str:
        nonlocal model_root_calls
        model_root_calls += 1
        assert language == "japan"
        return "/models"

    monkeypatch.setattr(ocr_local, "_rapidocr_model_root_dir", model_root)

    def cancelled() -> bool:
        return False

    async def run() -> None:
        async with rapidocr_execution_scope(
            language="japan", cancelled=cancelled, expected_rows=7
        ) as pool:
            recipe = OcrEngines(pool=pool)
            assert pool is _FakePool.instances[0]
            pages = await asyncio.create_task(
                recipe._ocr_rapidocr(
                    [Path("/tmp/page-1.png"), Path("/tmp/page-2.png")],
                    Path("/tmp/scratch"),
                    "japan",
                )
            )
            assert [page["text"] for page in pages] == ["pooled", "pooled"]
        assert pool.closed

    asyncio.run(run())

    pool = _FakePool.instances[0]
    assert model_root_calls == 1
    assert pool.kwargs == {
        "max_workers": 2,
        "expected_rows": 7,
        "language": "japan",
        "model_root_dir": "/models",
        "onnx_intra_threads": 2,
        "onnx_inter_threads": 1,
        "opencv_threads": 1,
        "should_cancel": cancelled,
        "memory_mb": 3584,
    }
    assert len(pool.paths) == 1
    assert pool.close_calls == 1
    assert lease.released == 1
    assert lease.poisoned == 0


def test_direct_call_requires_explicit_invocation_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _FakeLease()
    _install_fakes(monkeypatch, lease, workers=3)

    with pytest.raises(RuntimeError, match="invocation"):
        asyncio.run(
            OcrEngines()._ocr_rapidocr(
                [Path("/tmp/page.png")], Path("/tmp/scratch"), "korean"
            )
        )
    assert _FakePool.instances == []
    assert lease.released == 0


def test_busy_lease_is_a_typed_resumable_halt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ocr_local, "_rapidocr_model_root_dir", lambda language=None: None
    )
    monkeypatch.setattr(
        ocr_local, "rapidocr_topology", lambda expected_rows=None: _topology()
    )

    def busy(engine: str) -> Any:
        raise LocalEngineLeaseBusy("busy")

    monkeypatch.setattr(ocr_local, "acquire_local_engine_lease", busy)

    async def run() -> None:
        async with rapidocr_execution_scope(expected_rows=1, language=None):
            pass

    with pytest.raises(RecipeInvocationHalt) as caught:
        asyncio.run(run())
    assert caught.value.code == "local_engine_busy"


@pytest.mark.parametrize(
    ("error", "expected", "code"),
    [
        (RapidOCRSessionCancelled("cancel"), OcrCancelled, None),
        (
            RapidOCRSessionFailure("failed"),
            RecipeInvocationHalt,
            "local_session_failed",
        ),
        (RapidOCRRowError("bad image"), RuntimeError, None),
    ],
)
def test_pool_errors_map_at_recipe_boundary(
    error: BaseException,
    expected: type[BaseException],
    code: str | None,
) -> None:
    class FailingPool:
        async def ocr(self, paths: list[str]) -> list[dict[str, Any]]:
            raise error

    with pytest.raises(expected) as caught:
        asyncio.run(
            OcrEngines()._run_rapidocr_pool(
                FailingPool(),
                [Path("/tmp/page.png")],
            )
        )
    if code is not None:
        assert isinstance(caught.value, RecipeInvocationHalt)
        assert caught.value.code == code


def test_unverified_pool_teardown_poisons_lease_and_escapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _FakeLease()
    _install_fakes(monkeypatch, lease)
    _FakePool.teardown_on_close = True
    _FakePool.close_error = SandboxTeardownError("tree remained live")

    async def run() -> None:
        async with rapidocr_execution_scope(expected_rows=1, language=None):
            pass

    with pytest.raises(SandboxTeardownError):
        asyncio.run(run())
    assert lease.poisoned == 1
    assert lease.released == 0


def test_missing_runtime_fails_before_local_engine_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ocr_local,
        "rapidocr_available",
        lambda: (False, "install the RapidOCR runtime"),
    )

    def forbidden(language: str | None = None) -> Any:
        raise AssertionError("model probe must not run without the package")

    monkeypatch.setattr(ocr_local, "rapidocr_model_requirements", forbidden)
    with pytest.raises(RecipeInvocationHalt) as caught:
        ocr_local._rapidocr_model_root_dir()
    assert caught.value.code == "local_artifact_unavailable"
    assert "install" in caught.value.detail


def test_runtime_requires_one_complete_model_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package-models"
    shared_root = tmp_path / "shared-models"
    package_root.mkdir()
    shared_root.mkdir()
    filenames = ("det.onnx", "cls.onnx", "rec.onnx")
    monkeypatch.setattr(ocr_local, "rapidocr_available", lambda: (True, None))
    requested_languages: list[str | None] = []

    def requirements(language: str | None = None):
        requested_languages.append(language)
        return package_root, filenames

    monkeypatch.setattr(
        ocr_local,
        "rapidocr_model_requirements",
        requirements,
    )
    monkeypatch.setattr(
        ocr_local, "rapidocr_shared_model_cache_dir", lambda: shared_root
    )

    # A partial shared set is rejected, as is a complete set split across roots.
    for filename in filenames[1:]:
        (shared_root / filename).write_bytes(b"model")
    with pytest.raises(RecipeInvocationHalt) as caught:
        ocr_local._rapidocr_model_root_dir("japan")
    assert caught.value.code == "local_artifact_unavailable"
    (package_root / filenames[0]).write_bytes(b"det")
    with pytest.raises(RecipeInvocationHalt):
        ocr_local._rapidocr_model_root_dir("japan")

    # Completing the shared root selects it; a complete package takes priority.
    (shared_root / filenames[0]).write_bytes(b"det")
    assert ocr_local._rapidocr_model_root_dir("japan") == str(shared_root)
    for filename in filenames[1:]:
        (package_root / filename).write_bytes(b"model")
    assert ocr_local._rapidocr_model_root_dir("japan") is None
    assert requested_languages == ["japan"] * 4


def test_model_requirements_replace_only_requested_recognition_model() -> None:
    pytest.importorskip("rapidocr")
    default = rapidocr_model_requirements()
    japanese = rapidocr_model_requirements("japan")
    assert default is not None
    assert japanese is not None
    default_root, default_files = default
    japanese_root, japanese_files = japanese
    assert japanese_root == default_root
    assert japanese_files[:2] == default_files[:2]
    assert japanese_files[2] != default_files[2]
    assert "japan" in japanese_files[2]
    with pytest.raises(RapidOCRInvalidLanguage):
        rapidocr_model_requirements("klingon")


def test_missing_requested_language_model_halts_before_worker_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    shared_root = tmp_path / "shared"
    package_root.mkdir()
    shared_root.mkdir()
    filenames = ("det.onnx", "cls.onnx", "latin-rec.onnx")
    # Default files can exist while the specifically requested rec model does not.
    (package_root / filenames[0]).write_bytes(b"det")
    (package_root / filenames[1]).write_bytes(b"cls")
    monkeypatch.setattr(ocr_local, "rapidocr_available", lambda: (True, None))

    def requirements(language: str | None = None):
        assert language == "latin"
        return package_root, filenames

    monkeypatch.setattr(ocr_local, "rapidocr_model_requirements", requirements)
    monkeypatch.setattr(
        ocr_local, "rapidocr_shared_model_cache_dir", lambda: shared_root
    )

    lease = _FakeLease()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("missing language assets must fail before worker creation")

    monkeypatch.setattr(ocr_local, "rapidocr_topology", lambda *_: _topology())
    monkeypatch.setattr(ocr_local, "acquire_local_engine_lease", lambda *_: lease)
    monkeypatch.setattr(ocr_local, "RapidOCRProcessPool", forbidden)

    async def run() -> None:
        async with rapidocr_execution_scope(language="latin", expected_rows=2):
            pass

    with pytest.raises(RecipeInvocationHalt) as caught:
        asyncio.run(run())
    assert caught.value.code == "local_artifact_unavailable"
    assert lease.released == 1


def test_invalid_language_defers_to_worker_row_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ocr_local, "rapidocr_available", lambda: (True, None))

    def invalid(language: str | None = None):
        assert language == "klingon"
        raise RapidOCRInvalidLanguage(language)

    monkeypatch.setattr(ocr_local, "rapidocr_model_requirements", invalid)
    assert ocr_local._rapidocr_model_root_dir("klingon") is None


def test_pdf_rasterization_log_is_content_free(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from frisket.engine.sandbox.shim import SandboxResult

    source = tmp_path / "private-customer-invoice.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    scratch = tmp_path / "render"
    scratch.mkdir()

    async def fake_run_sandboxed(
        argv: list[str], *, policy: Any, should_cancel=None
    ) -> SandboxResult:
        assert argv[2:5] == ["-r", "144", str(source)]
        assert policy.memory_mb == 2048
        (scratch / "page-1.png").write_bytes(b"a" * 7)
        (scratch / "page-2.png").write_bytes(b"b" * 11)
        return SandboxResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ocr_mod.shutil, "which", lambda _executable: "/bin/pdftoppm")
    monkeypatch.setattr(ocr_mod, "run_sandboxed", fake_run_sandboxed)
    caplog.set_level(logging.INFO, logger="frisket.executor")

    pages = asyncio.run(
        OcrEngines()._page_images(
            source,
            {"mime": "application/pdf"},
            {"dpi": 144},
            scratch,
        )
    )

    assert [page.name for page in pages] == ["page-1.png", "page-2.png"]
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "ocr_pdf_rasterized"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.status == "ok"
    assert record.requested_dpi == 144
    assert record.duration_ms >= 0
    assert record.page_count == 2
    assert record.rendered_bytes == 18
    public_log = record.getMessage() + repr(
        {
            "event": record.event,
            "status": record.status,
            "requested_dpi": record.requested_dpi,
            "duration_ms": record.duration_ms,
            "page_count": record.page_count,
            "rendered_bytes": record.rendered_bytes,
        }
    )
    assert str(source) not in public_log
    assert "private-customer" not in public_log
