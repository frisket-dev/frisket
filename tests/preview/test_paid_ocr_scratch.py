"""Dropped OCR inputs use the shared paid-preview lifecycle without project rows."""

from __future__ import annotations

import asyncio
import io
import shutil
import json
import threading
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image, PngImagePlugin

from frisket.ai.llm import ModelRouter
from frisket.ai.llm.types import LLMResponse, SchemaViolation
from frisket.engine.store import Project
from frisket.execution.attempt_authority import attempt_identity
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.execution.resolve_for_action import consented_set_hash
from frisket.ops.base import OpContext
from frisket.ops.ocr_engines import OcrEngines
from frisket.engine.sandbox.shim import SandboxResult
import frisket.ops.ocr_engines as ocr_engines_module
from frisket.preview.ocr import OcrComparePreviewError
from frisket.server.services.scratch_ocr import paid_ocr_scratch_plan


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (10, 20), "white").save(output, format="PNG")
    return output.getvalue()


def _png_with_pdf_marker_metadata() -> bytes:
    output = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "%PDF- is metadata, not file magic")
    Image.new("RGB", (10, 20), "white").save(output, format="PNG", pnginfo=metadata)
    return output.getvalue()


def _composition(project):
    router = ModelRouter(keys={"openai": "test-key"}, cache=None, cache_mode="off")
    return router, open_execution_composition(
        project, router, ExecutionCompositionContext.direct()
    )


class _RunContext:
    def __init__(self, project, router):
        self.project = project
        self.router = router
        self.cancel_event = threading.Event()
        self.execution_limits = None
        self.calls = []
        self.progress_values = []

    @property
    def cancelled(self):
        return self.cancel_event.is_set()

    def op_context(self):
        return OpContext(
            project=self.project,
            http=self.router.client,
            extras={
                "preview": True,
                "router": self.router,
                "cancelled": self.cancel_event.is_set,
            },
        )

    def write_model_calls(self, calls):
        self.calls.extend(calls)

    def progress(self, done, total):
        self.progress_values.append((done, total))


class _InvalidStructuredAdapter:
    base_url = "http://unused.invalid"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request, client) -> LLMResponse:
        del client
        self.calls += 1
        return LLMResponse(
            content=json.dumps({}),
            data={},
            tokens_in=11,
            tokens_out=3,
            cost=0.02,
            model=request.model,
            provider="openai",
            credential_source="project_key",
            cost_source="provider_reported",
            duration_ms=7,
        )


def test_paid_ocr_image_plan_binds_source_and_returns_ephemeral_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(Project.create(tmp_path / "ocr.frisket")) as project:
        router, composition = _composition(project)
        source = _png()
        before = tuple(project.db.iterdump())
        plan, summary = paid_ocr_scratch_plan(
            project,
            media_bytes=source,
            payload={"engine": "openai/gpt-4.1-mini", "filename": "scan.png"},
            composition=composition,
        )
        assert summary == {
            "filename": "scan.png",
            "mime": None,
            "size": len(source),
            "kind": "image",
            "pages": [1],
        }
        assert plan.spec["params"]["dpi"] == 200
        assert plan.spec["params"]["source"] == "scratch"
        assert plan.work_scope["selection"] == {"pages": [1]}
        assert plan.source_identity["sha256"] == plan.work_scope["identity"]
        assert "pages" not in plan.estimate
        assert attempt_identity(plan.recipe, plan.spec)
        assert tuple(project.db.iterdump()) == before

        async def pages(self, path, media, options, scratch):
            assert path.read_bytes() == source
            assert media["mime"] == "image/png"
            assert options["dpi"] == 200
            rendered = scratch / "page-1.png"
            rendered.write_bytes(source)
            return [rendered]

        async def recognize(self, engine, page_paths, ctx, *, usage, **kwargs):
            assert engine == "openai/gpt-4.1-mini"
            assert len(page_paths) == 1
            assert ctx.extras["cancelled"]() is False
            usage["calls"] = 1
            usage["model_calls"] = [{"capability": "ocr", "engine": engine}]
            return [
                {
                    "text": "Invoice total 42",
                    "warnings": ["Page was rotated before recognition."],
                    "blocks": [
                        {
                            "text": "Invoice total 42",
                            "bbox": [[1, 2], [9, 2], [9, 10], [1, 10]],
                        }
                    ],
                }
            ]

        monkeypatch.setattr(OcrEngines, "_page_images", pages)
        monkeypatch.setattr(OcrEngines, "run_engine_on_pages", recognize)
        context = _RunContext(project, router)
        result = asyncio.run(plan.run(context))
        assert [column.name for column in result.columns] == [
            "page",
            "text",
            "blocks",
            "warnings",
            "errors",
            "runtime_ms",
        ]
        assert result.rows[0]["page"]["value"] == 1
        assert result.rows[0]["text"]["value"] == "Invoice total 42"
        assert result.rows[0]["blocks"]["value"][0]["bbox"] == {
            "space": "page_normalized",
            "x0": 0.1,
            "y0": 0.1,
            "x1": 0.9,
            "y1": 0.5,
        }
        assert result.rows[0]["warnings"]["value"] == [
            "Page was rotated before recognition."
        ]
        assert result.rows[0]["errors"]["value"] == []
        assert result.warnings == ("Page 1: Page was rotated before recognition.",)
        assert context.calls == [{"capability": "ocr", "engine": "openai/gpt-4.1-mini"}]
        assert context.progress_values == [(1, 1)]
        assert tuple(project.db.iterdump()) == before


def test_paid_ocr_pdf_searchable_output_is_an_ephemeral_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = b"%PDF-1.4\n% paid OCR fixture\n"
    with closing(Project.create(tmp_path / "ocr-pdf.frisket")) as project:
        router, composition = _composition(project)
        plan, _summary = paid_ocr_scratch_plan(
            project,
            media_bytes=source,
            payload={
                "engine": "tesseract",
                "filename": "docket.pdf",
                "pages": [1, 3],
                "dpi": 180,
                "searchable_pdf": True,
            },
            composition=composition,
        )

        def page_count(path, digest):
            assert path.read_bytes() == source
            assert len(digest) == 64
            return 3

        async def pages(self, path, media, options, scratch):
            assert options["_selected_pages"] == [1, 3]
            rendered = []
            for page in (1, 3):
                target = scratch / f"page-{page}.png"
                target.write_bytes(_png())
                rendered.append(target)
            return rendered

        async def recognize(self, engine, page_paths, ctx, *, usage, **kwargs):
            usage["model_calls"] = [{"capability": "ocr", "engine": engine}]
            return [
                {
                    "text": f"page {page}",
                    "blocks": [
                        {
                            "text": f"page {page}",
                            "bbox": [[1, 1], [5, 1], [5, 5], [1, 5]],
                        }
                    ],
                }
                for page in (1, 3)
            ]

        def compose(pdf, overlays, *, dpi):
            assert pdf == source and dpi == 180
            assert not overlays[1]
            return b"%PDF-searchable", [
                SimpleNamespace(index=0, degraded=False),
                SimpleNamespace(index=1, degraded=False),
                SimpleNamespace(index=2, degraded=True),
            ]

        monkeypatch.setattr(OcrEngines, "_page_images", pages)
        monkeypatch.setattr(OcrEngines, "_pdf_page_count", staticmethod(page_count))
        monkeypatch.setattr(OcrEngines, "run_engine_on_pages", recognize)
        monkeypatch.setattr(
            "frisket.ops.searchable_pdf.compose_searchable_pdf", compose
        )
        context = _RunContext(project, router)
        result = asyncio.run(plan.run(context))
        assert [row["page"]["value"] for row in result.rows] == [1, 3]
        assert result.rows[0]["pdf"]["value"].id in result.artifacts
        assert result.rows[1]["pdf"]["value"] is None
        artifact = next(iter(result.artifacts.values()))
        assert artifact.path.read_bytes() == b"%PDF-searchable"
        assert artifact.filename == "docket.searchable.pdf"
        assert result.warnings == ("Searchable PDF text was degraded on pages 3.",)
        result.close()
        assert not artifact.path.exists()


def test_paid_ocr_image_rejects_nonexistent_pages_before_resolution(
    tmp_path: Path,
) -> None:
    with closing(Project.create(tmp_path / "ocr-invalid.frisket")) as project:
        _router, composition = _composition(project)
        with pytest.raises(OcrComparePreviewError) as error:
            paid_ocr_scratch_plan(
                project,
                media_bytes=_png(),
                payload={"engine": "tesseract", "pages": [2]},
                composition=composition,
            )
        assert error.value.code == "invalid_page_ref"
        assert error.value.field == "pages"
        assert project.sheets(include_hidden=True) == []


def test_paid_ocr_png_metadata_pdf_marker_remains_an_image_at_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _png_with_pdf_marker_metadata()
    assert b"%PDF-" in source[:1024]
    with closing(Project.create(tmp_path / "ocr-png-metadata.frisket")) as project:
        router, composition = _composition(project)
        plan, summary = paid_ocr_scratch_plan(
            project,
            media_bytes=source,
            payload={"engine": "tesseract", "filename": "annotated.png"},
            composition=composition,
        )

        async def recognize(self, engine, page_paths, ctx, *, usage, **kwargs):
            assert engine == "tesseract"
            assert [path.read_bytes() for path in page_paths] == [source]
            return [{"text": "image text", "blocks": []}]

        monkeypatch.setattr(OcrEngines, "run_engine_on_pages", recognize)
        result = asyncio.run(plan.run(_RunContext(project, router)))

        assert summary["kind"] == "image"
        assert result.rows[0]["text"]["value"] == "image text"


def test_paid_ocr_confirmation_hash_is_bound_to_page_selection(
    tmp_path: Path,
) -> None:
    source = b"%PDF-1.4\n"
    with closing(Project.create(tmp_path / "ocr-selection.frisket")) as project:
        _router, composition = _composition(project)
        first, _summary = paid_ocr_scratch_plan(
            project,
            media_bytes=source,
            payload={"engine": "openai/gpt-4.1-mini", "pages": [1]},
            composition=composition,
        )
        second, _summary = paid_ocr_scratch_plan(
            project,
            media_bytes=source,
            payload={"engine": "openai/gpt-4.1-mini", "pages": [2]},
            composition=composition,
        )

        assert consented_set_hash(
            first.resolved_execution.promise_set
        ) != consented_set_hash(second.resolved_execution.promise_set)


def test_paid_ocr_all_page_failures_raise_after_usage_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(Project.create(tmp_path / "ocr-failed.frisket")) as project:
        router, composition = _composition(project)
        plan, _summary = paid_ocr_scratch_plan(
            project,
            media_bytes=_png(),
            payload={"engine": "openai/gpt-4.1-mini", "filename": "blank.png"},
            composition=composition,
        )

        async def recognize(self, engine, page_paths, ctx, *, usage, **kwargs):
            usage["model_calls"] = [{"capability": "ocr", "engine": engine}]
            return [{"text": "", "blocks": [], "errors": ["provider refused page"]}]

        monkeypatch.setattr(OcrEngines, "run_engine_on_pages", recognize)
        context = _RunContext(project, router)
        with pytest.raises(OcrComparePreviewError) as error:
            asyncio.run(plan.run(context))
        assert error.value.code == "ocr_preview_engine_failed"
        assert error.value.details == {"failed_pages": [1]}
        assert context.calls == [{"capability": "ocr", "engine": "openai/gpt-4.1-mini"}]


def test_paid_ocr_structured_failure_retains_billed_wire_usage(
    tmp_path: Path,
) -> None:
    with closing(Project.create(tmp_path / "ocr-schema-failed.frisket")) as project:
        router, composition = _composition(project)
        adapter = _InvalidStructuredAdapter()
        router._adapters["openai"] = adapter  # noqa: SLF001
        plan, _summary = paid_ocr_scratch_plan(
            project,
            media_bytes=_png(),
            payload={"engine": "openai/gpt-4.1-mini", "filename": "invalid.png"},
            composition=composition,
        )
        context = _RunContext(project, router)

        with pytest.raises(SchemaViolation):
            asyncio.run(plan.run(context))

        assert adapter.calls == 2
        assert len(context.calls) == 1
        [fact] = context.calls
        assert fact["capability"] == "ocr"
        assert fact["provider_cost_usd"] == pytest.approx(0.04)
        assert fact["units"]["pages"] == 1
        assert fact["units"]["requests"] == 1
        assert fact["units"]["tokens_in"] == 22
        assert fact["units"]["tokens_out"] == 6
        assert fact["units"]["input_bytes"] == len(_png())
        assert isinstance(fact["duration_ms"], int)
        assert fact["duration_ms"] >= 0


def test_pdf_renderer_rasterizes_only_selected_pages_in_original_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "large.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    commands: list[list[str]] = []

    async def run_sandboxed(
        argv: list[str], *, policy: Any, should_cancel=None
    ) -> SandboxResult:
        del policy, should_cancel
        commands.append(argv)
        Path(f"{argv[-1]}.png").write_bytes(_png())
        return SandboxResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(OcrEngines, "_is_pdf", staticmethod(lambda path, media: True))
    monkeypatch.setattr(
        ocr_engines_module.shutil, "which", lambda _executable: "/bin/pdftoppm"
    )
    monkeypatch.setattr(ocr_engines_module, "run_sandboxed", run_sandboxed)

    paths = asyncio.run(
        OcrEngines()._page_images(
            source,
            {"mime": "application/pdf"},
            {"dpi": 150, "_selected_pages": [1000, 1]},
            scratch,
        )
    )

    assert [path.name for path in paths] == ["page-1000.png", "page-1.png"]
    assert len(commands) == 2
    assert [
        (command[command.index("-f") + 1], command[command.index("-l") + 1])
        for command in commands
    ] == [
        ("1000", "1000"),
        ("1", "1"),
    ]
    assert all("-singlefile" in command for command in commands)


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="Poppler is optional")
def test_selected_page_render_uses_real_pdf_page(tmp_path: Path) -> None:
    source = tmp_path / "colors.pdf"
    Image.new("RGB", (32, 32), "red").save(
        source,
        format="PDF",
        save_all=True,
        append_images=[Image.new("RGB", (32, 32), "blue")],
    )
    scratch = tmp_path / "rendered"
    scratch.mkdir()
    paths = asyncio.run(
        OcrEngines()._page_images(
            source,
            {"mime": "application/pdf"},
            {"dpi": 72, "_selected_pages": [2]},
            scratch,
        )
    )
    assert [path.name for path in paths] == ["page-2.png"]
    assert list(scratch.glob("*.png")) == paths
    with Image.open(paths[0]) as image:
        red, green, blue = image.convert("RGB").getpixel((16, 16))
    assert blue > 200 and red < 30 and green < 30
