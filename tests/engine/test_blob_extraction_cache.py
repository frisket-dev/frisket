"""Project-scoped blob extraction cache for media.ocr and media.transcribe."""

from __future__ import annotations

import asyncio
from decimal import Decimal
import io
import json
from pathlib import Path
import sqlite3
from typing import Any

from PIL import Image
from pypdf import PdfWriter
import pytest

from frisket.actions.media_options import OcrOptions
from frisket.actions.system import typed_action_for_request
from frisket.actions.types import ColumnRef, Row
from frisket.ai.llm import ModelRouter
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.executor.ocr_read import AdmittedOcrReader
from frisket.engine.executor.row_file_stage import RowFileStager
from frisket.engine.runner import MapRunner
from frisket.engine.runner.validation import ClaimsGate
from frisket.engine.store import Project
from frisket.engine.store.extraction_cache import (
    EXTRACTION_CACHE_KEY_VERSION,
    EXTRACTION_CACHE_SOURCE,
    BlobExtractionCache,
    ExtractionTarget,
    build_extraction_cache_key,
    cache_db_path,
    ocr_engine_version,
    page_selector_for,
)
from frisket.engine.store.execution_routes import instance_principal
from frisket.engine.store.media_blobs import media_cell, owned_media_metadata_document
from frisket.engine.store.project_blobs import gc_blobs, reclaim_local_blobs
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.transactions import write_transaction
from frisket.execution.attempt import ATTEMPT_EXTRA
from frisket.execution.attempt_authority import AttemptAuthority
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
from frisket.ops import ocr_engines as ocr_engines_mod
from frisket.ops import ocr_engines_local as ocr_local
from frisket.ops import ocr_engines_remote as ocr_remote
from frisket.ops.base import ActionContext, ActionProgramInvocationHalt
import frisket.sdk.ops.transcribe_engines as transcribe_engines


def _make_pdf_bytes(page_count: int = 2) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=200, height=300)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_png_bytes(
    width: int = 20, height: int = 10, color: tuple[int, int, int] = (255, 255, 255)
) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _make_wav_bytes(tag: bytes = b"audio-1") -> bytes:
    return b"RIFF0000WAVEfmt " + tag


def _seed_pdf_sheet(
    project: Project,
    *,
    name: str,
    digest: str,
    filename: str = "filing.pdf",
    page: int | None = None,
) -> tuple[int, int, int]:
    sheet_id = project.add_sheet(name)
    col_id = project.add_column(sheet_id, "document", type="file")
    cell = media_cell(digest, mime="application/pdf", filename=filename)
    if page is not None:
        cell["page"] = page
    [row_id] = project.add_rows(sheet_id, [{"document": cell}], {"document": col_id})
    return sheet_id, col_id, row_id


def _seed_audio_sheet(
    project: Project,
    *,
    name: str,
    digest: str,
    filename: str = "clip.wav",
) -> tuple[int, int, int]:
    sheet_id = project.add_sheet(name)
    col_id = project.add_column(sheet_id, "recording", type="audio")
    cell = media_cell(digest, mime="audio/wav", filename=filename)
    [row_id] = project.add_rows(sheet_id, [{"recording": cell}], {"recording": col_id})
    return sheet_id, col_id, row_id


def _ocr_request(
    sheet_id: int,
    *,
    row_ids: list[int] | None = None,
    engine: str = "rapidocr",
    dpi: int = 200,
    language: str | None = None,
    searchable_pdf: bool = False,
    output_prefix: str = "ocr",
    idempotency_key: str,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "source": "document",
        "engine": engine,
        "dpi": dpi,
        "searchable_pdf": searchable_pdf,
    }
    if language is not None:
        params["language"] = language
    outputs: dict[str, str] = {
        "text": f"{output_prefix}_text",
        "blocks": f"{output_prefix}_blocks",
    }
    if searchable_pdf:
        outputs["pdf"] = f"{output_prefix}_pdf"
    scope: dict[str, Any] = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = row_ids
    return {
        "action_id": "media.ocr",
        "scope": scope,
        "params": params,
        "output_names": outputs,
        "idempotency_key": idempotency_key,
    }


def _transcribe_request(
    sheet_id: int,
    *,
    row_ids: list[int] | None = None,
    engine: str = "faster_whisper",
    language: list[str] | None = None,
    vad: bool | None = None,
    output_prefix: str = "transcript",
    idempotency_key: str,
) -> dict[str, Any]:
    params: dict[str, Any] = {"source": "recording", "engine": engine}
    if language is not None:
        params["language"] = language
    if vad is not None:
        params["vad"] = vad
    scope: dict[str, Any] = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = row_ids
    return {
        "action_id": "media.transcribe",
        "scope": scope,
        "params": params,
        "output_names": {
            "text": output_prefix,
            "segments": f"{output_prefix}_segments",
        },
        "idempotency_key": idempotency_key,
    }


def _estimate_typed_action(
    project: Project,
    request: dict[str, Any],
    *,
    router: ModelRouter,
    threshold_usd: Decimal = Decimal("0"),
) -> tuple[MapRunner, Any, dict[str, Any]]:
    bound = typed_action_for_request(request)
    plan = build_typed_map_rows_plan(project, bound, _admit_output_targets=False)
    composition = open_execution_composition(
        project, router, ExecutionCompositionContext.direct()
    )
    coverage = ConsentCoverage(instance_principal(project), threshold_usd)
    runner = MapRunner(
        project,
        router,
        authority=AttemptAuthority(
            project,
            composition=composition,
            consent_coverage=coverage,
        ),
        execution_composition=composition,
        consent_coverage=coverage,
        allow_action_lifecycle_only_programs=True,
    )
    est = runner.estimate(plan.spec_dict(), program=plan.program)
    return runner, plan, est


def _patch_local_ocr(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[dict[str, Any]],
    *,
    text_prefix: str = "Page text",
) -> None:
    png_bytes = _make_png_bytes(100, 200)

    async def fake_page_images(
        self: ocr_engines_mod.OcrEngines,
        path: Path,
        media: Any,
        spec: dict[str, Any],
        scratch: Path,
        *,
        engine: str | None = None,
    ) -> list[Path]:
        del self, path, media, engine
        selected = spec.get("_selected_pages") or [1]
        out: list[Path] = []
        for idx in selected:
            rendered = scratch / f"page-{idx}.png"
            rendered.write_bytes(png_bytes)
            out.append(rendered)
        return out

    async def fake_rapidocr(
        self: ocr_engines_mod.OcrEngines,
        page_paths: list[Path],
        scratch: Path,
        language: str | None = None,
    ) -> list[dict[str, Any]]:
        del self, scratch
        calls.append({"pages": len(page_paths), "language": language})
        return [
            {
                "text": f"{text_prefix} {idx} lang={language or 'auto'}",
                "width": 100,
                "height": 200,
                "blocks": [
                    {
                        "text": f"{text_prefix} {idx}",
                        "bbox": [[10, 20], [90, 20], [90, 60], [10, 60]],
                        "score": 0.98,
                    }
                ],
            }
            for idx, _path in enumerate(page_paths, 1)
        ]

    monkeypatch.setattr(ocr_engines_mod.OcrEngines, "_page_images", fake_page_images)
    monkeypatch.setattr(ocr_engines_mod.OcrEngines, "_ocr_rapidocr", fake_rapidocr)
    monkeypatch.setattr(
        ocr_local, "_rapidocr_model_root_dir", lambda language=None: None
    )


def _patch_local_transcribe(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[dict[str, Any]],
    *,
    text_box: list[str] | None = None,
) -> None:
    async def fake_transcribe(
        self: transcribe_engines.FasterWhisperAdapter,
        path: str,
        spec: dict[str, Any],
        *,
        should_cancel: Any = None,
    ) -> dict[str, Any]:
        del self, should_cancel
        calls.append(
            {
                "path": Path(path).name,
                "language": spec.get("language"),
                "vad": spec.get("vad"),
            }
        )
        prefix = text_box[0] if text_box else "Transcript"
        return {
            "text": f"{prefix} hello world",
            "segments": [
                {"start": 0.0, "end": 1.25, "text": f"{prefix} hello"},
                {"start": 1.25, "end": 2.5, "text": "world"},
            ],
            "language": "en",
            "duration": 2.5,
        }

    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter, "transcribe", fake_transcribe
    )


def test_ocr_cross_sheet_cache_hit_and_independent_citations_and_review(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    _patch_local_ocr(monkeypatch, calls)

    project = make_project("ocr-cross-sheet")
    pdf_bytes = _make_pdf_bytes(page_count=2)
    digest = project.add_blob(
        pdf_bytes,
        filename="filing.pdf",
        mime="application/pdf",
        metadata=owned_media_metadata_document(probe={"kind": "pdf", "pages": 2}),
    )
    sheet_a, _col_a, row_a = _seed_pdf_sheet(
        project, name="Sheet A", digest=digest, filename="filing_a.pdf"
    )
    sheet_b, _col_b, row_b = _seed_pdf_sheet(
        project, name="Sheet B", digest=digest, filename="filing_b.pdf"
    )
    router = ModelRouter(model_call_policy=LOCAL_PURCHASE_POLICY, cache_mode="replay")

    first = run_action_spec(
        project,
        _ocr_request(sheet_a, idempotency_key="ocr-sheet-a"),
        project_id="ocr-cross-sheet",
        router=router,
    )
    assert first.status == "completed", first.errors
    assert len(calls) == 2

    # Estimate on Sheet B proves 1 cached row, 0 live rows.
    _runner, _plan, est_b = _estimate_typed_action(
        project,
        _ocr_request(sheet_b, idempotency_key="ocr-sheet-b-est"),
        router=router,
    )
    assert est_b["cached_rows"] == 1
    assert est_b["live_rows"] == 0
    assert est_b["cost"] == 0.0

    # Sheet B runs OCR with 0 engine calls and writes independent artifacts/citations/review state.
    second = run_action_spec(
        project,
        _ocr_request(sheet_b, idempotency_key="ocr-sheet-b"),
        project_id="ocr-cross-sheet",
        router=router,
    )
    assert second.status == "completed", second.errors
    assert len(calls) == 2

    mc_b = project.db.execute(
        "SELECT credential_source, cost_source, provider_cost_usd, duration_ms, cache, units "
        "FROM model_calls WHERE run_id=?",
        (second.run_id,),
    ).fetchall()
    assert len(mc_b) == 1
    assert mc_b[0]["credential_source"] == "cache"
    assert mc_b[0]["cost_source"] == "cache_hit"
    assert mc_b[0]["provider_cost_usd"] == 0.0
    assert mc_b[0]["duration_ms"] is None
    assert json.loads(mc_b[0]["cache"]) == {
        "hit": True,
        "source": EXTRACTION_CACHE_SOURCE,
    }
    assert json.loads(mc_b[0]["units"]) == {"pages": 0}

    col_text_a = next(
        c["id"] for c in project.columns(sheet_a) if c["name"] == "ocr_text"
    )
    col_text_b = next(
        c["id"] for c in project.columns(sheet_b) if c["name"] == "ocr_text"
    )
    val_a = project.get_values(sheet_a, col_text_a, row_ids=[row_a])[row_a]
    val_b = project.get_values(sheet_b, col_text_b, row_ids=[row_b])[row_b]
    assert val_a == val_b
    assert "Page text 1" in val_b

    # Independent source_artifacts, prepared_content_refs, and citations per sheet.
    art_a = project.db.execute(
        "SELECT id FROM source_artifacts WHERE source_sheet_id=? AND source_row_id=?",
        (sheet_a, row_a),
    ).fetchone()
    art_b = project.db.execute(
        "SELECT id FROM source_artifacts WHERE source_sheet_id=? AND source_row_id=?",
        (sheet_b, row_b),
    ).fetchone()
    assert art_a is not None and art_b is not None
    assert art_a["id"] != art_b["id"]

    cit_a = project.db.execute(
        "SELECT id FROM citations WHERE sheet_id=? AND row_id=? AND link_role='media_ocr_grounding'",
        (sheet_a, row_a),
    ).fetchone()
    cit_b = project.db.execute(
        "SELECT id FROM citations WHERE sheet_id=? AND row_id=? AND link_role='media_ocr_grounding'",
        (sheet_b, row_b),
    ).fetchone()
    assert cit_a is not None and cit_b is not None
    assert cit_a["id"] != cit_b["id"]

    res_a = project.db.execute(
        "SELECT value_kind, value, review_state FROM results WHERE run_id=? AND row_id=? AND column_id=?",
        (first.run_id, row_a, col_text_a),
    ).fetchone()
    res_b = project.db.execute(
        "SELECT value_kind, value, review_state FROM results WHERE run_id=? AND row_id=? AND column_id=?",
        (second.run_id, row_b, col_text_b),
    ).fetchone()
    assert res_a["value_kind"] == "prepared_content_ref"
    assert res_b["value_kind"] == "prepared_content_ref"
    assert res_a["value"] != res_b["value"]
    assert res_a["review_state"] == "unreviewed"
    assert res_b["review_state"] == "unreviewed"

    # Marking Sheet A reviewed MUST NOT mark Sheet B reviewed.
    with write_transaction(project.db, what="review sheet a"):
        RunResultStore(project).set_result_review_state(
            first.run_id, row_a, col_text_a, "verified"
        )
    after_a = project.db.execute(
        "SELECT review_state FROM results WHERE run_id=? AND row_id=? AND column_id=?",
        (first.run_id, row_a, col_text_a),
    ).fetchone()
    after_b = project.db.execute(
        "SELECT review_state FROM results WHERE run_id=? AND row_id=? AND column_id=?",
        (second.run_id, row_b, col_text_b),
    ).fetchone()
    assert after_a["review_state"] == "verified"
    assert after_b["review_state"] == "unreviewed"


def test_transcribe_cross_sheet_cache_hit_preserves_segments_and_review_isolation(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    _patch_local_transcribe(monkeypatch, calls)

    project = make_project("asr-cross-sheet")
    digest = project.add_blob(
        _make_wav_bytes(b"interview-1"),
        filename="interview.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"kind": "audio", "duration_seconds": 2.5}
        ),
    )
    sheet_a, _col_a, row_a = _seed_audio_sheet(
        project, name="Sheet A", digest=digest, filename="a.wav"
    )
    sheet_b, _col_b, row_b = _seed_audio_sheet(
        project, name="Sheet B", digest=digest, filename="b.wav"
    )
    router = ModelRouter(model_call_policy=LOCAL_PURCHASE_POLICY, cache_mode="replay")

    first = run_action_spec(
        project,
        _transcribe_request(sheet_a, idempotency_key="asr-sheet-a"),
        project_id="asr-cross-sheet",
        router=router,
    )
    assert first.status == "completed", first.errors
    assert len(calls) == 1

    _runner, _plan, est_b = _estimate_typed_action(
        project,
        _transcribe_request(sheet_b, idempotency_key="asr-sheet-b-est"),
        router=router,
    )
    assert est_b["cached_rows"] == 1
    assert est_b["live_rows"] == 0
    assert est_b["cost"] == 0.0

    second = run_action_spec(
        project,
        _transcribe_request(sheet_b, idempotency_key="asr-sheet-b"),
        project_id="asr-cross-sheet",
        router=router,
    )
    assert second.status == "completed", second.errors
    assert len(calls) == 1

    mc_b = project.db.execute(
        "SELECT credential_source, cost_source, provider_cost_usd, duration_ms, cache "
        "FROM model_calls WHERE run_id=?",
        (second.run_id,),
    ).fetchall()
    assert len(mc_b) == 1
    assert mc_b[0]["credential_source"] == "cache"
    assert mc_b[0]["cost_source"] == "cache_hit"
    assert mc_b[0]["provider_cost_usd"] == 0.0
    assert mc_b[0]["duration_ms"] is None
    assert json.loads(mc_b[0]["cache"]) == {
        "hit": True,
        "source": EXTRACTION_CACHE_SOURCE,
    }

    cols_a = {c["name"]: c["id"] for c in project.columns(sheet_a)}
    cols_b = {c["name"]: c["id"] for c in project.columns(sheet_b)}
    assert (
        project.get_values(sheet_b, cols_b["transcript"], row_ids=[row_b])[row_b]
        == "Transcript hello world"
    )
    segs_b = project.get_values(
        sheet_b, cols_b["transcript_segments"], row_ids=[row_b]
    )[row_b]
    assert len(segs_b) == 2
    assert segs_b[0]["start"] == 0.0
    assert segs_b[0]["end"] == 1.25
    assert segs_b[1]["start"] == 1.25
    assert segs_b[1]["end"] == 2.5

    cit_b = project.db.execute(
        "SELECT id FROM citations WHERE sheet_id=? AND row_id=? AND link_role='media_transcribe_temporal'",
        (sheet_b, row_b),
    ).fetchone()
    assert cit_b is not None

    with write_transaction(project.db, what="review sheet a transcript"):
        RunResultStore(project).set_result_review_state(
            first.run_id, row_a, cols_a["transcript"], "verified"
        )
    rev_b = project.db.execute(
        "SELECT review_state FROM results WHERE run_id=? AND row_id=? AND column_id=?",
        (second.run_id, row_b, cols_b["transcript"]),
    ).fetchone()
    assert rev_b["review_state"] == "unreviewed"


def test_cache_key_sensitivity_and_cross_project_isolation(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    _patch_local_ocr(monkeypatch, calls)

    # Pure key sensitivity unit checks: engine, engine_version, DPI, language, page selector.
    base_target = ExtractionTarget(
        capability="ocr", engine="rapidocr", transport="local"
    )
    base_key = build_extraction_cache_key(
        base_target,
        blob_hash="a" * 64,
        normalized_options={"dpi": 200, "searchable_pdf": False},
        page_selector=page_selector_for(None),
    )
    diff_engine = build_extraction_cache_key(
        ExtractionTarget(capability="ocr", engine="tesseract", transport="local"),
        blob_hash="a" * 64,
        normalized_options={"dpi": 200, "searchable_pdf": False},
        page_selector=page_selector_for(None),
    )
    diff_version = build_extraction_cache_key(
        ExtractionTarget(
            capability="ocr",
            engine="rapidocr",
            transport="local",
            engine_version="rapidocr:v999:other",
        ),
        blob_hash="a" * 64,
        normalized_options={"dpi": 200, "searchable_pdf": False},
        page_selector=page_selector_for(None),
    )
    assert ocr_engine_version("rapidocr", transport="local") != "rapidocr:v999:other"
    diff_dpi = build_extraction_cache_key(
        base_target,
        blob_hash="a" * 64,
        normalized_options={"dpi": 300, "searchable_pdf": False},
        page_selector=page_selector_for(None),
    )
    diff_lang = build_extraction_cache_key(
        base_target,
        blob_hash="a" * 64,
        normalized_options={"dpi": 200, "searchable_pdf": False, "language": "en"},
        page_selector=page_selector_for(None),
    )
    diff_page = build_extraction_cache_key(
        base_target,
        blob_hash="a" * 64,
        normalized_options={"dpi": 200, "searchable_pdf": False},
        page_selector=page_selector_for(1),
    )
    diff_key_ver = build_extraction_cache_key(
        base_target,
        blob_hash="a" * 64,
        normalized_options={"dpi": 200, "searchable_pdf": False},
        page_selector=page_selector_for(None),
        cache_key_version=EXTRACTION_CACHE_KEY_VERSION + 1,
    )
    digests = {
        k.digest
        for k in (
            base_key,
            diff_engine,
            diff_version,
            diff_dpi,
            diff_lang,
            diff_page,
            diff_key_ver,
        )
    }
    assert len(digests) == 7

    # End-to-end misses when DPI, language, or page selector changes.
    project = make_project("key-sensitivity")
    pdf_bytes = _make_pdf_bytes(page_count=2)
    digest = project.add_blob(
        pdf_bytes,
        filename="doc.pdf",
        mime="application/pdf",
        metadata=owned_media_metadata_document(probe={"kind": "pdf", "pages": 2}),
    )
    sheet_whole, _, _ = _seed_pdf_sheet(project, name="Whole", digest=digest)
    sheet_p1, _, _ = _seed_pdf_sheet(project, name="Page1", digest=digest, page=1)
    sheet_p2, _, _ = _seed_pdf_sheet(project, name="Page2", digest=digest, page=2)
    router = ModelRouter(model_call_policy=LOCAL_PURCHASE_POLICY, cache_mode="replay")

    r1 = run_action_spec(
        project,
        _ocr_request(sheet_whole, dpi=200, language="en", idempotency_key="k1"),
        project_id="key-sensitivity",
        router=router,
    )
    assert r1.status == "completed", r1.errors
    assert len(calls) == 2

    # Different DPI -> cache MISS
    r2 = run_action_spec(
        project,
        _ocr_request(
            sheet_whole,
            dpi=300,
            language="en",
            output_prefix="ocr_300",
            idempotency_key="k2",
        ),
        project_id="key-sensitivity",
        router=router,
    )
    assert r2.status == "completed", r2.errors
    assert len(calls) == 4

    # Different language -> cache MISS
    r3 = run_action_spec(
        project,
        _ocr_request(
            sheet_whole,
            dpi=200,
            language="fr",
            output_prefix="ocr_fr",
            idempotency_key="k3",
        ),
        project_id="key-sensitivity",
        router=router,
    )
    assert r3.status == "completed", r3.errors
    assert len(calls) == 6

    # Page 1 vs Page 2 -> distinct cache keys, both MISS against whole-doc key
    r4 = run_action_spec(
        project,
        _ocr_request(sheet_p1, dpi=200, language="en", idempotency_key="k4"),
        project_id="key-sensitivity",
        router=router,
    )
    assert r4.status == "completed", r4.errors
    assert len(calls) == 7

    r5 = run_action_spec(
        project,
        _ocr_request(sheet_p2, dpi=200, language="en", idempotency_key="k5"),
        project_id="key-sensitivity",
        router=router,
    )
    assert r5.status == "completed", r5.errors
    assert len(calls) == 8

    # Cross-project isolation: Project 2 with identical blob bytes MUST MISS.
    other = make_project("isolated-other")
    other_digest = other.add_blob(
        pdf_bytes,
        filename="doc.pdf",
        mime="application/pdf",
        metadata=owned_media_metadata_document(probe={"kind": "pdf", "pages": 2}),
    )
    assert other_digest == digest
    other_sheet, _, _ = _seed_pdf_sheet(other, name="Other", digest=other_digest)
    r_other = run_action_spec(
        other,
        _ocr_request(other_sheet, dpi=200, language="en", idempotency_key="k-other"),
        project_id="isolated-other",
        router=router,
    )
    assert r_other.status == "completed", r_other.errors
    assert len(calls) == 10


def test_rerun_fresh_bypasses_cache_read_and_updates_cache(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    text_box = ["First"]
    _patch_local_transcribe(monkeypatch, calls, text_box=text_box)

    project = make_project("fresh-bypass")
    digest = project.add_blob(
        _make_wav_bytes(b"fresh-1"),
        filename="clip.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"kind": "audio", "duration_seconds": 2.5}
        ),
    )
    sheet_a, _, _ = _seed_audio_sheet(project, name="A", digest=digest)
    sheet_b, _, row_b = _seed_audio_sheet(project, name="B", digest=digest)
    sheet_c, _, row_c = _seed_audio_sheet(project, name="C", digest=digest)

    replay_router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY, cache_mode="replay"
    )
    fresh_router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY, cache_mode="fresh"
    )

    r1 = run_action_spec(
        project,
        _transcribe_request(sheet_a, idempotency_key="fresh-r1"),
        project_id="fresh-bypass",
        router=replay_router,
    )
    assert r1.status == "completed", r1.errors
    assert len(calls) == 1

    # Fresh estimate must report 0 cached rows.
    _runner, _plan, est_fresh = _estimate_typed_action(
        project,
        _transcribe_request(sheet_b, idempotency_key="fresh-est"),
        router=fresh_router,
    )
    assert est_fresh["cached_rows"] == 0
    assert est_fresh["live_rows"] == 1

    # Fresh run bypasses cache read, calls engine, and writes updated result to cache.
    text_box[0] = "Refreshed"
    r2 = run_action_spec(
        project,
        _transcribe_request(sheet_b, idempotency_key="fresh-r2"),
        project_id="fresh-bypass",
        router=fresh_router,
    )
    assert r2.status == "completed", r2.errors
    assert len(calls) == 2
    col_b = next(c["id"] for c in project.columns(sheet_b) if c["name"] == "transcript")
    assert (
        project.get_values(sheet_b, col_b, row_ids=[row_b])[row_b]
        == "Refreshed hello world"
    )

    # Subsequent replay on Sheet C hits the newly written cached result with 0 engine calls.
    r3 = run_action_spec(
        project,
        _transcribe_request(sheet_c, idempotency_key="fresh-r3"),
        project_id="fresh-bypass",
        router=replay_router,
    )
    assert r3.status == "completed", r3.errors
    assert len(calls) == 2
    col_c = next(c["id"] for c in project.columns(sheet_c) if c["name"] == "transcript")
    assert (
        project.get_values(sheet_c, col_c, row_ids=[row_c])[row_c]
        == "Refreshed hello world"
    )


def test_purge_and_gc_delete_blob_cache_entries_and_corrupt_sidecar_is_safe(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    _patch_local_transcribe(monkeypatch, calls)

    project = make_project("purge-cache")
    digest_1 = project.add_blob(
        _make_wav_bytes(b"blob-one"),
        filename="one.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"kind": "audio", "duration_seconds": 2.5}
        ),
    )
    digest_2 = project.add_blob(
        _make_wav_bytes(b"blob-two"),
        filename="two.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"kind": "audio", "duration_seconds": 2.5}
        ),
    )
    sheet_1, _, row_1 = _seed_audio_sheet(project, name="S1", digest=digest_1)
    sheet_2, _, row_2 = _seed_audio_sheet(project, name="S2", digest=digest_2)
    router = ModelRouter(model_call_policy=LOCAL_PURCHASE_POLICY, cache_mode="replay")

    assert (
        run_action_spec(
            project,
            _transcribe_request(sheet_1, idempotency_key="p1"),
            project_id="purge-cache",
            router=router,
        ).status
        == "completed"
    )
    assert (
        run_action_spec(
            project,
            _transcribe_request(sheet_2, idempotency_key="p2"),
            project_id="purge-cache",
            router=router,
        ).status
        == "completed"
    )
    sidecar = cache_db_path(project)
    assert sidecar is not None and sidecar.exists()

    def _cached_blob_hashes() -> set[str]:
        conn = sqlite3.connect(sidecar)
        try:
            return {
                str(r[0])
                for r in conn.execute(
                    "SELECT DISTINCT blob_hash FROM blob_extraction_cache"
                ).fetchall()
            }
        finally:
            conn.close()

    assert _cached_blob_hashes() == {digest_1, digest_2}

    # Add an unreferenced blob + cache entry and verify gc_blobs prunes only its cache entry.
    orphan_digest = project.add_blob(
        _make_wav_bytes(b"orphan"),
        filename="orphan.wav",
        mime="audio/wav",
    )
    orphan_target = ExtractionTarget(
        capability="transcribe", engine="faster_whisper", transport="local"
    )
    orphan_key = build_extraction_cache_key(
        orphan_target,
        blob_hash=orphan_digest,
        normalized_options={},
    )
    cache = BlobExtractionCache.for_project(project)
    assert cache is not None
    with cache:
        cache.put(
            orphan_key,
            {"text": "orphan", "segments": [], "language": "en", "duration": 1.0},
        )
    assert orphan_digest in _cached_blob_hashes()

    gc_result = gc_blobs(project, dry_run=False)
    assert orphan_digest in gc_result["hashes"]
    assert _cached_blob_hashes() == {digest_1, digest_2}

    # Also verify reclaim_local_blobs prunes cache rows when unreferenced blobs are reclaimed.
    orphan_2 = project.add_blob(
        _make_wav_bytes(b"orphan-2"),
        filename="orphan2.wav",
        mime="audio/wav",
    )
    orphan_2_key = build_extraction_cache_key(
        orphan_target,
        blob_hash=orphan_2,
        normalized_options={},
    )
    with cache:
        cache.put(
            orphan_2_key,
            {"text": "orphan2", "segments": [], "language": "en", "duration": 1.0},
        )
    assert orphan_2 in _cached_blob_hashes()
    project.close()
    reopened = Project(project.path, maintenance=True)
    try:
        reclaim_local_blobs(reopened, dry_run=False)
        assert orphan_2 not in _cached_blob_hashes()
        assert {digest_1, digest_2} <= _cached_blob_hashes()
    finally:
        reopened.close()

    reopened = Project(project.path)
    try:
        # Deleting project.cache.db leaves project.db 100% healthy.
        sidecar.unlink()
        assert not sidecar.exists()
        col_1 = next(
            c["id"] for c in reopened.columns(sheet_1) if c["name"] == "transcript"
        )
        assert (
            reopened.get_values(sheet_1, col_1, row_ids=[row_1])[row_1]
            == "Transcript hello world"
        )

        # Corrupt project.cache.db never breaks reads or subsequent runs.
        sidecar.write_bytes(b"not a valid sqlite database")
        sheet_3, _, row_3 = _seed_audio_sheet(reopened, name="S3", digest=digest_2)
        before_calls = len(calls)
        r3 = run_action_spec(
            reopened,
            _transcribe_request(sheet_3, idempotency_key="after-corrupt"),
            project_id="purge-cache",
            router=router,
        )
        assert r3.status == "completed", r3.errors
        assert len(calls) == before_calls + 1
        col_3 = next(
            c["id"] for c in reopened.columns(sheet_3) if c["name"] == "transcript"
        )
        assert (
            reopened.get_values(sheet_3, col_3, row_ids=[row_3])[row_3]
            == "Transcript hello world"
        )
        assert row_2 > 0
    finally:
        reopened.close()


def test_adr_0002_paid_ocr_cache_deleted_between_estimate_and_run_never_spends(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting project.cache.db after a $0 cached estimate MUST NEVER trigger
    an unconsented paid external OCR call — both pre-prepare AND post-prepare!"""
    monkeypatch.setenv("DATALAB_API_KEY", "test-datalab-key")
    paid_calls: list[int] = []
    png_bytes = _make_png_bytes(100, 100)

    async def fake_page_images(
        self: ocr_engines_mod.OcrEngines,
        path: Path,
        media: Any,
        spec: dict[str, Any],
        scratch: Path,
        *,
        engine: str | None = None,
    ) -> list[Path]:
        del self, path, media, spec, engine
        out = scratch / "page-1.png"
        out.write_bytes(png_bytes)
        return [out]

    async def fake_datalab_ocr(
        pages: list[Path], ctx: Any, usage: dict[str, Any]
    ) -> list[dict[str, Any]]:
        del ctx
        paid_calls.append(len(pages))
        usage["calls"] = usage.get("calls", 0) + 1
        usage["cost"] = 0.01
        usage["credential_source"] = "local"
        return [
            {
                "text": "Paid Datalab Text",
                "width": 100,
                "height": 100,
                "blocks": [],
            }
            for _ in pages
        ]

    monkeypatch.setattr(ocr_engines_mod.OcrEngines, "_page_images", fake_page_images)
    monkeypatch.setattr(ocr_remote, "ocr_datalab", fake_datalab_ocr)

    project = make_project("adr0002-ocr")
    digest = project.add_blob(
        _make_pdf_bytes(page_count=1),
        filename="paid.pdf",
        mime="application/pdf",
        metadata=owned_media_metadata_document(probe={"kind": "pdf", "pages": 1}),
    )
    sheet_a, _, _ = _seed_pdf_sheet(project, name="Sheet A", digest=digest)
    sheet_b, _, row_b = _seed_pdf_sheet(project, name="Sheet B", digest=digest)
    sheet_c, _, row_c = _seed_pdf_sheet(project, name="Sheet C", digest=digest)
    router = ModelRouter(model_call_policy=LOCAL_PURCHASE_POLICY, cache_mode="replay")

    deps = ExecutorDeps(
        consent_coverage=ConsentCoverage(instance_principal(project), Decimal("0")),
    )

    # 1. Seed cache via Sheet A (explicitly confirmed paid run).
    req_a = _ocr_request(sheet_a, engine="datalab", idempotency_key="paid-ocr-a")
    _runner_a, _plan_a, est_a = _estimate_typed_action(
        project, req_a, router=router, threshold_usd=Decimal("0")
    )
    assert est_a["cached_rows"] == 0
    assert est_a["live_rows"] == 1
    assert est_a["cost"] > 0
    res_a = run_action_spec(
        project,
        {**req_a, "confirmation": est_a["promise_set_hash"]},
        project_id="adr0002-ocr",
        router=router,
        deps=deps,
    )
    assert res_a.status == "completed", res_a.errors
    assert len(paid_calls) == 1

    # 2. Estimate on Sheet B while cache is warm -> $0, 1 cached row, 0 live rows.
    req_b = _ocr_request(sheet_b, engine="datalab", idempotency_key="paid-ocr-b")
    runner_b, plan_b, est_b = _estimate_typed_action(
        project, req_b, router=router, threshold_usd=Decimal("0")
    )
    assert est_b["cached_rows"] == 1
    assert est_b["live_rows"] == 0
    assert est_b["cost"] == 0.0

    # 3. Scenario A: Delete project.cache.db BEFORE run() -> ClaimsGate / needs_confirmation!
    sidecar = cache_db_path(project)
    assert sidecar is not None and sidecar.exists()
    cached_backup = sidecar.read_bytes()
    sidecar.unlink()

    spec_b = dict(plan_b.spec_dict())
    if est_b.get("promise_set_hash"):
        spec_b["consented_promise_set_hash"] = est_b["promise_set_hash"]
    with pytest.raises(ClaimsGate):
        asyncio.run(runner_b.run(spec_b, program=plan_b.program, confirmed=True))
    res_b_gated = run_action_spec(
        project,
        req_b,
        project_id="adr0002-ocr",
        router=router,
        deps=deps,
    )
    assert res_b_gated.status == "needs_confirmation"
    assert len(paid_calls) == 1

    # 4. Scenario B: Restore cache, prepare_run() (persisting $0 route/consent),
    #    THEN delete project.cache.db BEFORE attempt admission -> MUST halt with promise_violation!
    sidecar.write_bytes(cached_backup)
    req_c = _ocr_request(sheet_c, engine="datalab", idempotency_key="paid-ocr-c")
    runner_c, plan_c, est_c = _estimate_typed_action(
        project, req_c, router=router, threshold_usd=Decimal("0")
    )
    assert est_c["cached_rows"] == 1
    assert est_c["live_rows"] == 0
    assert est_c["cost"] == 0.0
    spec_c = dict(plan_c.spec_dict())
    if est_c.get("promise_set_hash"):
        spec_c["consented_promise_set_hash"] = est_c["promise_set_hash"]
    prepared = runner_c.prepare_run(spec_c, program=plan_c.program, confirmed=True)
    assert prepared.run_id is not None

    # Delete project.cache.db AFTER prepare_run persisted the $0 consent!
    sidecar.unlink()
    with pytest.raises(ActionProgramInvocationHalt) as halted:
        runner_c.authority.mint(
            program=plan_c.program,
            spec=spec_c,
            run_id=prepared.run_id,
            scope=(row_c,),
        )
    assert halted.value.code == "promise_violation"
    assert len(paid_calls) == 1

    # 5. Scenario C (Mid-run TOCTOU): Even after attempt admission succeeded while
    #    cache existed, deleting project.cache.db right before row execution halts
    #    before touching the paid provider!
    sidecar.write_bytes(cached_backup)
    commitment = runner_c.authority.mint(
        program=plan_c.program,
        spec=spec_c,
        run_id=prepared.run_id,
        scope=(row_c,),
    )
    sidecar.unlink()

    col_doc_c = next(
        c["id"] for c in project.columns(sheet_c) if c["name"] == "document"
    )
    cell_c = project.get_values(sheet_c, col_doc_c, row_ids=[row_c])[row_c]
    ctx_c = ActionContext(
        project=project,
        extras={
            ATTEMPT_EXTRA: commitment,
            "router": router,
            "cancelled": lambda: False,
        },
    )
    options_c = OcrOptions(dpi=200, searchable_pdf=False)
    reader = AdmittedOcrReader(
        ctx_c,
        RowFileStager(project, preview=False),
        engine="datalab",
        options=options_c.normalize("datalab"),
    )

    async def _run_toctou() -> None:
        await reader.start(expected_rows=1)
        try:
            row_obj = Row({"document": cell_c})
            bound = reader.bind_row(
                row_obj,
                sheet_id=sheet_c,
                row_id=row_c,
                sources={
                    "document": {
                        "column_id": col_doc_c,
                        "column_type": "file",
                        "value": cell_c,
                    }
                },
                ctx=ctx_c,
            )
            await bound.recognize(row_obj, ColumnRef("document"), options=options_c)
        finally:
            await reader.aclose()

    with pytest.raises(ActionProgramInvocationHalt) as mid_run_halted:
        asyncio.run(_run_toctou())
    assert mid_run_halted.value.code == "promise_violation"
    assert len(paid_calls) == 1
    assert row_b > 0 and row_c > 0


def test_adr_0002_paid_transcribe_cache_deleted_between_estimate_and_run_never_spends(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting project.cache.db after a $0 cached estimate for paid remote ASR
    MUST NEVER trigger an unconsented paid external transcription API call."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    paid_calls: list[str] = []

    async def fake_hosted_transcribe(
        self: Any,
        engine: str,
        path: str,
        spec: dict[str, Any],
        ctx: Any,
        media: Any = None,
    ) -> dict[str, Any]:
        del self, engine, spec, ctx, media
        paid_calls.append(Path(path).name)
        return {
            "text": "Paid Whisper Transcript",
            "segments": [{"start": 0.0, "end": 10.0, "text": "Paid Whisper"}],
            "language": "en",
            "duration": 10.0,
            "cost": 0.001,
            "credential_source": "local",
        }

    monkeypatch.setattr(
        transcribe_engines.OpenAITranscriptionAdapter,
        "transcribe",
        fake_hosted_transcribe,
    )

    project = make_project("adr0002-asr")
    digest = project.add_blob(
        _make_wav_bytes(b"paid-audio"),
        filename="paid.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"kind": "audio", "duration_seconds": 10.0}
        ),
    )
    sheet_a, _, _ = _seed_audio_sheet(project, name="A", digest=digest)
    sheet_b, _, row_b = _seed_audio_sheet(project, name="B", digest=digest)
    router = ModelRouter(model_call_policy=LOCAL_PURCHASE_POLICY, cache_mode="replay")
    deps = ExecutorDeps(
        consent_coverage=ConsentCoverage(instance_principal(project), Decimal("0")),
    )

    # 1. Seed cache via Sheet A (confirmed paid run).
    req_a = _transcribe_request(
        sheet_a, engine="openai/whisper-1", idempotency_key="paid-asr-a"
    )
    _runner_a, _plan_a, est_a = _estimate_typed_action(
        project, req_a, router=router, threshold_usd=Decimal("0")
    )
    assert est_a["cached_rows"] == 0
    assert est_a["live_rows"] == 1
    assert est_a["cost"] > 0
    res_a = run_action_spec(
        project,
        {**req_a, "confirmation": est_a["promise_set_hash"]},
        project_id="adr0002-asr",
        router=router,
        deps=deps,
    )
    assert res_a.status == "completed", res_a.errors
    assert len(paid_calls) == 1

    # 2. Prepare Sheet B while cache is warm -> $0 estimate.
    req_b = _transcribe_request(
        sheet_b, engine="openai/whisper-1", idempotency_key="paid-asr-b"
    )
    runner_b, plan_b, est_b = _estimate_typed_action(
        project, req_b, router=router, threshold_usd=Decimal("0")
    )
    assert est_b["cached_rows"] == 1
    assert est_b["live_rows"] == 0
    assert est_b["cost"] == 0.0
    assert est_b["audio_seconds"] == 0.0
    spec_b = dict(plan_b.spec_dict())
    if est_b.get("promise_set_hash"):
        spec_b["consented_promise_set_hash"] = est_b["promise_set_hash"]
    prepared = runner_b.prepare_run(spec_b, program=plan_b.program, confirmed=True)

    # 3. Delete project.cache.db after prepare_run -> MUST refuse before API call!
    sidecar = cache_db_path(project)
    assert sidecar is not None and sidecar.exists()
    sidecar.unlink()
    with pytest.raises(ActionProgramInvocationHalt) as halted:
        runner_b.authority.mint(
            program=plan_b.program,
            spec=spec_b,
            run_id=prepared.run_id,
            scope=(row_b,),
        )
    assert halted.value.code == "promise_violation"
    assert len(paid_calls) == 1


def test_partial_cache_hit_quotes_only_live_rows(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With 1 cached row and 1 uncached row, Estimate quotes ONLY the live row."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    async def fake_hosted_transcribe(
        self: Any,
        engine: str,
        path: str,
        spec: dict[str, Any],
        ctx: Any,
        media: Any = None,
    ) -> dict[str, Any]:
        del self, engine, path, spec, ctx, media
        return {
            "text": "Cached one",
            "segments": [{"start": 0.0, "end": 60.0, "text": "Cached one"}],
            "language": "en",
            "duration": 60.0,
            "cost": 0.006,
            "credential_source": "local",
        }

    monkeypatch.setattr(
        transcribe_engines.OpenAITranscriptionAdapter,
        "transcribe",
        fake_hosted_transcribe,
    )

    project = make_project("partial-cache")
    d1 = project.add_blob(
        _make_wav_bytes(b"part-1"),
        filename="p1.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"kind": "audio", "duration_seconds": 60.0}
        ),
    )
    d2 = project.add_blob(
        _make_wav_bytes(b"part-2"),
        filename="p2.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"kind": "audio", "duration_seconds": 60.0}
        ),
    )
    sheet_a, _, _ = _seed_audio_sheet(project, name="A", digest=d1)
    sheet_b = project.add_sheet("B")
    col_b = project.add_column(sheet_b, "recording", type="audio")
    project.add_rows(
        sheet_b,
        [
            {"recording": media_cell(d1, mime="audio/wav", filename="p1.wav")},
            {"recording": media_cell(d2, mime="audio/wav", filename="p2.wav")},
        ],
        {"recording": col_b},
    )
    router = ModelRouter(model_call_policy=LOCAL_PURCHASE_POLICY, cache_mode="replay")

    # Full uncached estimate for 2 x 60s rows = 120s.
    req_b = _transcribe_request(
        sheet_b, engine="openai/whisper-1", idempotency_key="partial-b"
    )
    _, _, est_uncached = _estimate_typed_action(
        project, req_b, router=router, threshold_usd=Decimal("0")
    )
    assert est_uncached["cached_rows"] == 0
    assert est_uncached["live_rows"] == 2
    assert est_uncached["audio_seconds"] == 120.0

    # Cache d1 via Sheet A.
    req_a = _transcribe_request(
        sheet_a, engine="openai/whisper-1", idempotency_key="partial-a"
    )
    _runner_a, _plan_a, est_a = _estimate_typed_action(
        project, req_a, router=router, threshold_usd=Decimal("0")
    )
    deps = ExecutorDeps(
        consent_coverage=ConsentCoverage(instance_principal(project), Decimal("0")),
    )
    res_a = run_action_spec(
        project,
        {**req_a, "confirmation": est_a["promise_set_hash"]},
        project_id="partial-cache",
        router=router,
        deps=deps,
    )
    assert res_a.status == "completed", res_a.errors

    # Now Sheet B quotes 1 cached row + 1 live row = 60s = half cost!
    _, _, est_partial = _estimate_typed_action(
        project, req_b, router=router, threshold_usd=Decimal("0")
    )
    assert est_partial["cached_rows"] == 1
    assert est_partial["live_rows"] == 1
    assert est_partial["audio_seconds"] == 60.0
    assert est_partial["cost"] == pytest.approx(est_uncached["cost"] / 2)


def test_undo_redo_sheet_b_cached_run_preserves_sheet_a_and_cache(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    _patch_local_ocr(monkeypatch, calls)

    project = make_project("undo-redo-cache")
    digest = project.add_blob(
        _make_pdf_bytes(page_count=1),
        filename="undo.pdf",
        mime="application/pdf",
        metadata=owned_media_metadata_document(probe={"kind": "pdf", "pages": 1}),
    )
    sheet_a, _, row_a = _seed_pdf_sheet(project, name="Sheet A", digest=digest)
    sheet_b, _, row_b = _seed_pdf_sheet(project, name="Sheet B", digest=digest)
    router = ModelRouter(model_call_policy=LOCAL_PURCHASE_POLICY, cache_mode="replay")

    first = run_action_spec(
        project,
        _ocr_request(sheet_a, idempotency_key="undo-a"),
        project_id="undo-redo-cache",
        router=router,
    )
    assert first.status == "completed", first.errors
    assert len(calls) == 1

    second = run_action_spec(
        project,
        _ocr_request(sheet_b, idempotency_key="undo-b"),
        project_id="undo-redo-cache",
        router=router,
    )
    assert second.status == "completed", second.errors
    assert len(calls) == 1

    col_a = next(c["id"] for c in project.columns(sheet_a) if c["name"] == "ocr_text")
    col_b = next(c["id"] for c in project.columns(sheet_b) if c["name"] == "ocr_text")
    assert "Page text 1" in project.get_values(sheet_a, col_a, row_ids=[row_a])[row_a]
    assert "Page text 1" in project.get_values(sheet_b, col_b, row_ids=[row_b])[row_b]

    # Undo Sheet B's run via the standard operation.undo action.
    undone = run_action_spec(
        project,
        {
            "action_id": "operation.undo",
            "scope": {"kind": "project"},
            "params": {"expected_op_id": second.op_ids[0]},
            "idempotency_key": "undo-sheet-b-op",
        },
        project_id="undo-redo-cache",
        router=router,
    )
    assert undone.status == "completed", undone.errors

    # Sheet B's generated column is hidden; Sheet A is untouched; cache still intact!
    visible_b = {c["name"] for c in project.columns(sheet_b)}
    assert "ocr_text" not in visible_b
    assert "Page text 1" in project.get_values(sheet_a, col_a, row_ids=[row_a])[row_a]
    sidecar = cache_db_path(project)
    assert sidecar is not None and sidecar.exists()

    # Redo restores Sheet B's column and value with 0 engine calls.
    redone = run_action_spec(
        project,
        {
            "action_id": "operation.redo",
            "scope": {"kind": "project"},
            "params": {"expected_op_id": second.op_ids[0]},
            "idempotency_key": "redo-sheet-b-op",
        },
        project_id="undo-redo-cache",
        router=router,
    )
    assert redone.status == "completed", redone.errors
    assert "Page text 1" in project.get_values(sheet_b, col_b, row_ids=[row_b])[row_b]
    assert len(calls) == 1
