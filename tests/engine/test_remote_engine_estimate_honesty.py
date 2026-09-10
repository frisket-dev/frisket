from __future__ import annotations

import asyncio
import json

import pytest

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.runner import CostGate, MapRunner
from frisket.engine.runner.validation import ExecutionResolutionRefused
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import media_cell, owned_media_metadata_document
from frisket.operability.trace import read_trace
from frisket.execution.attempt_authority import (
    UnroutedOnlyAuthority,
)
from tests.execution_composition_helpers import open_attempt_authority


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "p.frisket")
    yield p
    p.close()


def _ocr_project(p: Project, *, value: str | dict = "/nonexistent/scan.png"):
    sheet = p.add_sheet("data")
    cols = {"page": p.add_column(sheet, "page", type="image")}
    p.add_rows(sheet, [{"page": value}], cols)
    return sheet


def _ocr_spec(sheet: int, engine: str) -> dict:
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import _typed_map_rows_plan

    return _typed_map_rows_plan(
        typed_action_for_request(
            {
                "action_id": "media.ocr",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {"source": "page", "engine": engine},
                "idempotency_key": "estimate-honesty",
            }
        )
    ).spec_dict()


def _transcribe_project(p: Project, *, value: str = "/nonexistent/a.wav"):
    sheet = p.add_sheet("data")
    cols = {"audio": p.add_column(sheet, "audio", type="audio")}
    p.add_rows(sheet, [{"audio": value}], cols)
    return sheet


def _transcribe_spec(sheet: int, engine: str) -> dict:
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import _typed_map_rows_plan

    return _typed_map_rows_plan(
        typed_action_for_request(
            {
                "action_id": "media.transcribe",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {"source": "audio", "engine": engine},
                "idempotency_key": "estimate-honesty",
            }
        )
    ).spec_dict()


def _program(spec):
    from frisket.engine.executor.map_rows_action import (
        _typed_map_rows_plan,
        bound_typed_program_request_from_runner_spec,
    )

    return _typed_map_rows_plan(
        bound_typed_program_request_from_runner_spec(spec)
    ).program


class _StubAdapter:
    """Records every call; returns a canned structured response — the
    per-row TracingRouter proxy calls this with ``trace=events`` like the
    real router does (frisket.llm.router.ModelRouter.complete)."""

    def __init__(self, reply: dict):
        self._reply = reply
        self.calls = 0

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.calls += 1
        return LLMResponse(
            content=json.dumps(self._reply),
            data=dict(self._reply),
            tokens_in=42,
            tokens_out=7,
            cost=0.0001,
            model=req.model,
        )


# ---------------------------------------------------------------------------
# (a) OCR: remote/VLM engine estimates None; local engine estimates 0.0


class TestOcrEstimateHonesty:
    def test_local_engine_estimate_is_honest_zero(self, project):
        sheet = _ocr_project(project)
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            allow_action_lifecycle_only_recipes=True,
            authority=UnroutedOnlyAuthority(project),
        )
        est = runner.estimate(
            _ocr_spec(sheet, "rapidocr"), program=_program(_ocr_spec(sheet, "rapidocr"))
        )
        assert est["cost"] == 0.0

    def test_sidecar_engine_estimate_is_honest_zero(self, project, monkeypatch):
        # self-hosted compute the operator already pays for — genuinely
        # zero, not unknown (matches transcribe.py's sidecar stance).
        # This is an available self-hosted route, not a missing deployment.
        monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test")
        monkeypatch.setenv("FRISKET_MODELS_TOKEN", "synthetic-gateway-token")
        sheet = _ocr_project(project)
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            allow_action_lifecycle_only_recipes=True,
            authority=UnroutedOnlyAuthority(project),
        )
        est = runner.estimate(
            _ocr_spec(sheet, "dots.mocr"),
            program=_program(_ocr_spec(sheet, "dots.mocr")),
        )
        assert est["cost"] == 0.0

    def test_remote_vlm_engine_estimate_is_unknown_not_zero(self, project, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "synthetic-ocr-key")
        sheet = _ocr_project(project)
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            allow_action_lifecycle_only_recipes=True,
            authority=UnroutedOnlyAuthority(project),
        )
        est = runner.estimate(
            _ocr_spec(sheet, "gemini/gemini-3.5-flash"),
            program=_program(_ocr_spec(sheet, "gemini/gemini-3.5-flash")),
        )
        assert est["cost"] is None, (
            "a remote VLM OCR engine must estimate None (unknown), never a "
            "fabricated 0.0 — the cost gate's ask-always branch depends on "
            "this (map_runner.py:223)"
        )


# ---------------------------------------------------------------------------
# (b) transcribe: same contract, remote engine vs free_local


class TestTranscribeEstimateHonesty:
    def test_free_local_engine_estimate_is_honest_zero(self, project):
        sheet = _transcribe_project(project)
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            allow_action_lifecycle_only_recipes=True,
            authority=UnroutedOnlyAuthority(project),
        )
        est = runner.estimate(
            _transcribe_spec(sheet, "faster_whisper"),
            program=_program(_transcribe_spec(sheet, "faster_whisper")),
        )
        assert est["cost"] == 0.0
        assert est["cost_source"] == "free_local"

    def test_remote_engine_estimate_is_unknown_not_zero(self, project, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-transcription-key")
        # value is a plain string (not a media dict), so duration metadata
        # is genuinely unknown — the honest answer is None, never 0.0.
        sheet = _transcribe_project(project)
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            allow_action_lifecycle_only_recipes=True,
            authority=UnroutedOnlyAuthority(project),
        )
        est = runner.estimate(
            _transcribe_spec(sheet, "openai/whisper-1"),
            program=_program(_transcribe_spec(sheet, "openai/whisper-1")),
        )
        assert est["cost"] is None


# ---------------------------------------------------------------------------
# (b2) to_markdown (Phase 4, mirroring OCR's three-arm honesty above): a
# resolution-aware ``estimate()`` renders each of the three CostBasis arms
# through the shared canonical projector — this drives that function for
# real (``runner.estimate`` -> ``validation.estimate_run`` ->
# ``resolve_for_action`` -> ``recipe.estimate(..., resolution=...)``), not a
# hand-built resolution stand-in.


@pytest.mark.parametrize("operation", ["estimate", "run"])
@pytest.mark.parametrize(
    ("kind", "engine", "remedy"),
    [
        ("ocr", "dots.mocr", "FRISKET_MODELS_URL"),
        ("ocr", "gemini/gemini-3.5-flash", "GEMINI_API_KEY"),
        ("transcribe", "openai/whisper-1", "OPENAI_API_KEY"),
    ],
)
def test_unavailable_route_refuses_before_quote_or_consent(
    project, monkeypatch, operation, kind, engine, remedy
):
    for key in (
        "FRISKET_MODELS_URL",
        "FRISKET_MODELS_TOKEN",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    sheet = _ocr_project(project) if kind == "ocr" else _transcribe_project(project)
    spec = (
        _ocr_spec(sheet, engine) if kind == "ocr" else _transcribe_spec(sheet, engine)
    )
    runner = MapRunner(
        project,
        ModelRouter(cache=None, cache_mode="off"),
        allow_action_lifecycle_only_recipes=True,
        authority=UnroutedOnlyAuthority(project),
    )
    before = project.db.total_changes
    with pytest.raises(ExecutionResolutionRefused, match=remedy):
        if operation == "estimate":
            runner.estimate(spec, program=_program(spec))
        else:
            asyncio.run(runner.run(spec, program=_program(spec)))
    assert project.db.total_changes == before


def _to_markdown_project(p: Project, *, doc, pages: int | None = 2):
    sheet = p.add_sheet("data")
    col = p.add_column(sheet, "doc", type="file")
    if isinstance(doc, bytes):
        probe: dict = {"kind": "document"}
        if pages is not None:
            probe["pages"] = pages
        blob = p.add_blob(
            doc,
            filename="d.pdf",
            mime="application/pdf",
            metadata=owned_media_metadata_document(probe=probe),
        )
        value = media_cell(blob, mime="application/pdf", filename="d.pdf")
    else:
        value = doc
    p.add_rows(sheet, [{"doc": value}], {"doc": col})
    return sheet


def _to_markdown_estimate(runner: MapRunner, sheet: int, engine: str) -> dict:
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan

    plan = build_typed_map_rows_plan(
        runner.project,
        typed_action_for_request(
            {
                "action_id": "media.to_markdown",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {"source": "doc", "engine": engine},
                "output_names": {"markdown": "converted"},
                "idempotency_key": "markdown-estimate-honesty",
            }
        ),
    )
    return runner.estimate(plan.spec_dict(), program=plan.program)


class TestToMarkdownEstimateHonesty:
    def test_local_engine_estimate_is_honest_zero(self, project):
        sheet = _to_markdown_project(project, doc="<html><body>hi</body></html>")
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            allow_action_lifecycle_only_recipes=True,
            authority=UnroutedOnlyAuthority(project),
        )
        est = _to_markdown_estimate(runner, sheet, "markitdown")
        assert est["cost"] == 0.0
        assert est["cost_source"] == "free_local"

    def test_priced_datalab_engine_estimate_carries_canonical_money_identity(
        self, project, monkeypatch
    ):
        monkeypatch.setenv("DATALAB_API_KEY", "secret")
        sheet = _to_markdown_project(project, doc=b"%PDF-1.4 two-page-doc")
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            allow_action_lifecycle_only_recipes=True,
            authority=UnroutedOnlyAuthority(project),
        )
        est = _to_markdown_estimate(runner, sheet, "datalab")
        assert est["cost"] == pytest.approx(0.02)  # 2 pages * $0.01/page
        assert est["cost_source"] == "pricing_data"
        assert est["pricing_key"] == "datalab.convert.page"
        assert est["engine"] == "datalab"
        assert "pages" not in est
        assert "units" not in est

    def test_datalab_engine_with_unprobed_page_count_estimate_is_unknown_not_zero(
        self, project, monkeypatch
    ):
        # A blob whose ingest probe recorded no page count at all — never
        # knowable before the run, so the honest answer is unpriceable
        # (None), never a fabricated number (and never a silent zero, which
        # a PDF that really did probe at 0 pages would also not deserve).
        monkeypatch.setenv("DATALAB_API_KEY", "secret")
        sheet = _to_markdown_project(project, doc=b"%PDF-1.4 unprobed-doc", pages=None)
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            allow_action_lifecycle_only_recipes=True,
            authority=UnroutedOnlyAuthority(project),
        )
        est = _to_markdown_estimate(runner, sheet, "datalab")
        assert est["cost"] is None
        assert est["cost_source"] == "unknown"


# ---------------------------------------------------------------------------
# (c) an unconfirmed remote-engine run raises CostGate — the gate ACTUALLY
# asks (this is the real end-to-end proof; (a)/(b) alone could pass with a
# fix that never wires into map_runner._prepare's confirmed check)


class TestUnconfirmedRemoteEngineRunGated:
    def test_ocr_remote_vlm_run_unconfirmed_raises_costgate(self, project, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "synthetic-ocr-key")
        sheet = _ocr_project(project)
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            allow_action_lifecycle_only_recipes=True,
            authority=UnroutedOnlyAuthority(project),
        )
        spec = _ocr_spec(sheet, "gemini/gemini-3.5-flash")
        with pytest.raises(CostGate) as exc:
            asyncio.run(runner.run(spec, program=_program(spec)))
        assert exc.value.estimate is None

    def test_transcribe_remote_run_unconfirmed_raises_costgate(
        self, project, monkeypatch
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-transcription-key")
        sheet = _transcribe_project(project)
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            allow_action_lifecycle_only_recipes=True,
            authority=UnroutedOnlyAuthority(project),
        )
        spec = _transcribe_spec(sheet, "openai/whisper-1")
        with pytest.raises(CostGate) as exc:
            asyncio.run(runner.run(spec, program=_program(spec)))
        assert exc.value.estimate is None

    def test_ocr_local_engine_preparation_is_not_gated(self, project):
        # control: a genuinely free local engine must NOT trip the gate —
        # proves the fix is engine-aware, not "always ask for ocr/transcribe"
        digest = project.add_blob(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
            filename="scan.png",
            mime="image/png",
        )
        sheet = _ocr_project(
            project, value=media_cell(digest, filename="scan.png", mime="image/png")
        )
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            # OCR resolves through the seam, so this control needs the
            # routed authority — with UnroutedOnlyAuthority it would pass for
            # the wrong reason (a DependentChoiceRefusal, caught below).
            allow_action_lifecycle_only_recipes=True,
            authority=open_attempt_authority(project),
        )
        spec = _ocr_spec(sheet, "rapidocr")
        # Preparation owns admission; no model installation is needed to prove
        # the gate passes, and unrelated validation failures must not be swallowed.
        prepared = runner.prepare_run(spec, program=_program(spec))
        assert prepared.run_id > 0


# ---------------------------------------------------------------------------
# (d) a VLM OCR run's per-row trace contains the model call(s) — the
# TracingRouter must not be bypassed.


class TestVlmOcrTraceCapturesModelCalls:
    def test_vlm_ocr_default_trace_contains_model_call(
        self, project, tmp_path, monkeypatch
    ):
        # The remote-api venue must be LIVE to resolve to it.
        monkeypatch.setenv("GEMINI_API_KEY", "key")
        digest = project.add_blob(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
            filename="scan.png",
            mime="image/png",
        )
        sheet = _ocr_project(
            project, value=media_cell(digest, filename="scan.png", mime="image/png")
        )
        router = ModelRouter(cache=None, cache_mode="off")
        adapter = _StubAdapter({"text": "VLM READ 4271"})
        router._adapters["gemini"] = adapter  # noqa: SLF001 — stub injection, test-only

        runner = MapRunner(
            project,
            router,
            allow_action_lifecycle_only_recipes=True,
            authority=open_attempt_authority(project),
        )
        spec = _ocr_spec(sheet, "gemini/gemini-3.5-flash")
        # Confirmation is an exact challenge/echo, even in a direct runner
        # test. A bare boolean must not authorize whichever scope happens to
        # be live at retry time.
        with pytest.raises(CostGate) as challenge:
            asyncio.run(runner.run(spec, program=_program(spec), confirmed=True))
        assert challenge.value.promise_set_hash is not None
        confirmed_spec = {
            **spec,
            "consented_promise_set_hash": challenge.value.promise_set_hash,
        }
        from frisket.actions.system import BoundTypedActionRequest
        from frisket.engine.executor.map_rows_action import (
            bound_typed_program_request_from_runner_spec,
            run_typed_map_rows_action,
        )

        bound = bound_typed_program_request_from_runner_spec(confirmed_spec)
        confirmed_request = bound.request.model_copy(
            update={
                "confirmation": challenge.value.promise_set_hash,
            }
        )
        result = run_typed_map_rows_action(
            project,
            "test",
            BoundTypedActionRequest.bind(bound.action, confirmed_request),
            router,
            lambda project, router: runner,
        )
        assert result.status == "completed", (
            result.errors,
            [
                dict(row)
                for row in project.db.execute("SELECT error,error_code FROM results")
            ],
        )
        run_id = project.db.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        assert adapter.calls == 1, "the stub adapter never saw the VLM call"

        trace = read_trace(project.path, run_id)
        assert trace is not None, "debug run recorded no trace sidecar"
        (row,) = trace["rows"]
        assert row["calls"], (
            "per-row trace has no model calls — _ocr_vlm's router must be "
            "the per-row TracingRouter (map_runner.py _execute_row), not a "
            "raw router read straight from ctx.extras"
        )
        assert row["calls"][0]["model"] == "gemini/gemini-3.5-flash"
        assert row["calls"][0].get("raw_response")
