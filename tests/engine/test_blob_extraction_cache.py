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
from pypdf import PdfReader, PdfWriter
import pytest

from frisket.actions.media_options import OcrOptions, TranscriptionOptions
from frisket.actions.system import typed_action_for_request
from frisket.actions.types import ColumnRef, Row
from frisket.ai.llm import ModelRouter
from frisket.ai.models.metadata import ModelCallMeta
from frisket.cloud_targets import compose_cloud_targets
from frisket.contracts.http.cloud_connection import (
    CloudAccount,
    CloudCapabilitiesResponse,
    CloudCapabilityRoute,
    CloudOCROptionSupport,
    CloudTokenPrice,
)
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.executor.ocr_read import AdmittedOcrReader
from frisket.engine.executor.row_file_stage import RowFileStager
from frisket.engine.executor.transcription_read import AdmittedTranscriber
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
    probe_extraction_cache_rows,
)
from frisket.engine.store.execution_routes import instance_principal
from frisket.engine.store.media_blobs import media_cell, owned_media_metadata_document
from frisket.engine.store.project_blobs import gc_blobs, reclaim_local_blobs
from frisket.engine.store.purge import begin_purge, finish_purge
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.transactions import write_transaction
from frisket.execution.attempt import (
    ATTEMPT_EXTRA,
    cache_hit_units,
    run_attempt_receipts,
)
from frisket.execution.attempt_authority import AttemptAuthority, admit_routed
from frisket.execution.commercial import (
    CommercialEstimateRounding,
    CommercialEstimateTerms,
    CommercialOffering,
    CommercialOfferingMatch,
    CommercialPresentation,
    CommercialSettlementTerms,
)
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.execution.credential_use import CredentialUseContext
from frisket.execution.definitions import MODELS_GATEWAY_TARGET_ID, build_static_targets
from frisket.execution.price_book import PlatformMetered, settle
from frisket.execution.promise_compiler import PricedCostBasis, compile_route_promises
from frisket.execution.resolver import RouteRowFacts
from frisket.execution.provider import (
    CompositionFacts,
    ConnectionConfig,
    ExecutionComposition,
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
from frisket.ops import ocr_engines as ocr_engines_mod
from frisket.ops import ocr_engines_local as ocr_local
from frisket.ops import ocr_engines_remote as ocr_remote
from frisket.ops.base import ActionContext, ActionProgramInvocationHalt
import frisket.sdk.ops.transcribe_engines as transcribe_engines


def _make_pdf_bytes(page_count: int = 2, *, width: int = 200) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=width, height=300)
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
                "text": (
                    f"{text_prefix} {len(calls) if len(page_paths) == 1 else idx} "
                    f"lang={language or 'auto'}"
                ),
                "width": 100,
                "height": 200,
                "blocks": [
                    {
                        "text": f"{text_prefix} {len(calls) if len(page_paths) == 1 else idx}",
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


class _StaticTargetsProvider:
    def __init__(self) -> None:
        self._rows = tuple(build_static_targets())

    def targets(self) -> tuple[Any, ...]:
        return self._rows

    def connection(self, target_id: str) -> ConnectionConfig | None:
        return (
            ConnectionConfig(extra={"gpu": "L4"})
            if any(row.id == target_id for row in self._rows)
            else None
        )

    def configuration(self, target_id: str, capability: str, engine: str) -> str:
        del capability, engine
        return (
            "configured"
            if any(row.id == target_id for row in self._rows)
            else "not_configured"
        )

    def selection_rank(self, target_id: str, capability: str, engine: str) -> int:
        del target_id, capability, engine
        return 0


def _run_with_composition(
    project: Project,
    request: dict[str, Any],
    *,
    router: ModelRouter,
    composition: ExecutionComposition,
    threshold_usd: Decimal = Decimal("0"),
) -> tuple[int, dict[str, Any]]:
    bound = typed_action_for_request(request)
    plan = build_typed_map_rows_plan(project, bound, _admit_output_targets=False)
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
    req_with_confirm = (
        {**request, "confirmation": est["promise_set_hash"]}
        if est.get("promise_set_hash")
        else request
    )
    res = run_action_spec(
        project,
        req_with_confirm,
        project_id="cloud-ocr-settle",
        router=router,
        deps=ExecutorDeps(
            execution_composition=composition,
            consent_coverage=coverage,
        ),
    )
    assert res.status == "completed", res.errors
    assert res.run_id is not None
    return res.run_id, est


def test_settlement_on_cloud_ocr_and_custom_meter_key_offerings(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cache hits must populate the pinned meter_key so settle_attempt() settles
    to $0 on all-cached runs and exact live charge on partial-cached runs for
    both Frisket Cloud OCR (cloud_effective_cost_micro) and custom meter_key
    commercial offerings (gpu_seconds)."""
    cloud_calls: list[int] = []
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
            rendered = scratch / f"cloud-page-{idx}.png"
            rendered.write_bytes(png_bytes)
            out.append(rendered)
        return out

    async def fake_ocr_vlm(
        model: str,
        page_paths: list[Path],
        ctx: Any,
        usage: dict[str, Any],
        language: str | None = None,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        del ctx, language, _kwargs
        cloud_calls.append(len(page_paths))
        call = ModelCallMeta(
            capability="ocr",
            engine=model,
            provider="openai",
            provider_kind="platform_api",
            model_ids=["gpt-5-mini"],
            credential_source="platform_key",
            cost_source="cloud_receipt",
            provider_cost_usd=0.125,
            units={
                "pages": len(page_paths),
                "cloud_effective_cost_micro": 125_000,
            },
        ).as_dict()
        usage["calls"] = usage.get("calls", 0) + 1
        usage["cost"] = usage.get("cost", 0.0) + 0.125
        usage.setdefault("model_calls", []).append(call)
        return [
            {
                "text": f"Cloud VLM OCR page {idx}",
                "width": 100,
                "height": 200,
                "blocks": [
                    {
                        "text": f"Cloud VLM OCR page {idx}",
                        "bbox": [[10, 20], [90, 20], [90, 60], [10, 60]],
                        "score": 0.99,
                    }
                ],
            }
            for idx in range(1, len(page_paths) + 1)
        ]

    monkeypatch.setattr(ocr_engines_mod.OcrEngines, "_page_images", fake_page_images)
    monkeypatch.setattr(ocr_remote, "ocr_vlm", fake_ocr_vlm)

    project = make_project("cloud-ocr-settle")
    d1 = project.add_blob(
        _make_pdf_bytes(page_count=1),
        filename="c1.pdf",
        mime="application/pdf",
        metadata=owned_media_metadata_document(probe={"kind": "pdf", "pages": 1}),
    )
    d2 = project.add_blob(
        _make_pdf_bytes(page_count=1, width=250),
        filename="c2.pdf",
        mime="application/pdf",
        metadata=owned_media_metadata_document(probe={"kind": "pdf", "pages": 1}),
    )
    assert d2 != d1
    sheet_a, _, _ = _seed_pdf_sheet(project, name="Cloud A", digest=d1)
    sheet_b, _, _ = _seed_pdf_sheet(project, name="Cloud B", digest=d1)
    sheet_c = project.add_sheet("Cloud C")
    col_c = project.add_column(sheet_c, "document", type="file")
    project.add_rows(
        sheet_c,
        [
            {"document": media_cell(d1, mime="application/pdf", filename="c1.pdf")},
            {"document": media_cell(d2, mime="application/pdf", filename="c2.pdf")},
        ],
        {"document": col_c},
    )

    router = ModelRouter(model_call_policy=LOCAL_PURCHASE_POLICY, cache_mode="replay")
    base_comp = open_execution_composition(
        project, router, ExecutionCompositionContext.direct()
    )

    class _StubCloudTransport:
        async def execute_llm(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise AssertionError("unexpected direct execute_llm call")

    cloud_comp = compose_cloud_targets(
        base_comp,
        user_id=7,
        account=CloudAccount(
            id=41,
            name="Test Cloud",
            kind="personal",
            balance_micro=5_000_000,
            available_micro=5_000_000,
            manage_billing=True,
        ),
        capabilities=CloudCapabilitiesResponse(
            account_id=41,
            routes=[
                CloudCapabilityRoute(
                    capability="ocr",
                    engine="openai/gpt-5-mini",
                    provider="openai",
                    lane="frisket_credits",
                    route_revision="cloud-ocr-rev-1",
                    ready=True,
                    blocker=None,
                    price=CloudTokenPrice(
                        kind="tokens",
                        input_micro_per_million_tokens=100_000,
                        output_micro_per_million_tokens=200_000,
                    ),
                    options=CloudOCROptionSupport(
                        kind="ocr",
                        language=False,
                        geometry=True,
                        searchable_pdf=False,
                    ),
                )
            ],
        ),
        preference="cloud_first",
        execution_transport=_StubCloudTransport(),
        router=router,
    )

    # 1. Seed cache with Sheet A (1 live Cloud OCR call -> $0.125).
    run_a, est_a = _run_with_composition(
        project,
        _ocr_request(
            sheet_a, engine="openai/gpt-5-mini", idempotency_key="cloud-ocr-a"
        ),
        router=router,
        composition=cloud_comp,
    )
    assert est_a["cached_rows"] == 0
    assert est_a["live_rows"] == 1
    assert est_a["cost"] is None
    assert len(cloud_calls) == 1
    receipt_a = run_attempt_receipts(project, run_a)[-1]["settlement"]
    assert receipt_a["charge_usd"] == "0.125"
    assert receipt_a["unmetered_calls"] == 0

    # 2. All-cached Sheet B -> $0.00 estimate, 0 Cloud calls, settles to "0"!
    run_b, est_b = _run_with_composition(
        project,
        _ocr_request(
            sheet_b, engine="openai/gpt-5-mini", idempotency_key="cloud-ocr-b"
        ),
        router=router,
        composition=cloud_comp,
    )
    assert est_b["cached_rows"] == 1
    assert est_b["live_rows"] == 0
    assert est_b["cost"] == 0.0
    assert len(cloud_calls) == 1
    mc_b = project.db.execute(
        "SELECT units FROM model_calls WHERE run_id=?", (run_b,)
    ).fetchall()
    assert len(mc_b) == 1
    assert json.loads(mc_b[0]["units"]) == {
        "pages": 0,
        "cloud_effective_cost_micro": 0,
    }
    receipt_b = run_attempt_receipts(project, run_b)[-1]["settlement"]
    assert receipt_b["charge_usd"] == "0"
    assert receipt_b["rated_calls"] == 1
    assert receipt_b["unmetered_calls"] == 0
    assert "unsettleable" not in receipt_b

    # 3. Partial-cached Sheet C (1 cached + 1 live) -> cost=None at estimate,
    #    1 Cloud call on run, settles cleanly to exact live charge "$0.125"!
    run_c, est_c = _run_with_composition(
        project,
        _ocr_request(
            sheet_c, engine="openai/gpt-5-mini", idempotency_key="cloud-ocr-c"
        ),
        router=router,
        composition=cloud_comp,
    )
    assert est_c["cached_rows"] == 1
    assert est_c["live_rows"] == 1
    assert est_c["cost"] is None
    assert len(cloud_calls) == 2
    receipt_c = run_attempt_receipts(project, run_c)[-1]["settlement"]
    assert receipt_c["charge_usd"] == "0.125"
    assert receipt_c["rated_calls"] == 2
    assert receipt_c["unmetered_calls"] == 0
    assert "unsettleable" not in receipt_c

    # Part B: Commercial offering on media.transcribe with estimated_successful_rows
    # settlement + direct custom meter_key="gpu_seconds" settlement verification.
    gw_calls: list[str] = []

    async def fake_v1_transcribe(
        self: transcribe_engines.TranscriptionV1Adapter,
        engine: str,
        path: str,
        spec: dict[str, Any],
        ctx: Any,
        *,
        light_engine: str = "faster_whisper",
    ) -> dict[str, Any]:
        del self, engine, spec, ctx, light_engine
        gw_calls.append(Path(path).name)
        return {
            "text": "Gateway transcript",
            "segments": [{"start": 0.0, "end": 10.0, "text": "Gateway transcript"}],
            "language": "en",
            "duration": 10.0,
            "model_ids": ["parakeet-tdt"],
        }

    def fake_v1_model_calls(
        self: transcribe_engines.TranscriptionV1Adapter,
        engine: str,
        path: str,
        spec: dict[str, Any],
        out: dict[str, Any],
    ) -> list[dict[str, Any]]:
        del self, path, spec
        return [
            ModelCallMeta.sidecar(
                capability="transcribe",
                engine=engine,
                model_ids=list(out.get("model_ids") or [engine]),
                units={"audio_seconds": 10.0},
            ).as_dict()
        ]

    monkeypatch.setattr(
        transcribe_engines.TranscriptionV1Adapter, "transcribe", fake_v1_transcribe
    )
    monkeypatch.setattr(
        transcribe_engines.TranscriptionV1Adapter, "model_calls", fake_v1_model_calls
    )

    gw_offer = CommercialOffering(
        match=CommercialOfferingMatch(
            MODELS_GATEWAY_TARGET_ID, "transcribe", "parakeet-tdt"
        ),
        estimate=CommercialEstimateTerms(
            pricing_key="test.offer.audio_minute",
            terms_version="test.gw.v1",
            unit_rate="0.06",
            quantity_unit="audio_minute",
            rounding=CommercialEstimateRounding("half_even", 6),
        ),
        settlement=CommercialSettlementTerms(
            meter_key="audio_seconds",
            meter_units_per_quantity_unit="60",
            ceiling_mode="consented_quantity",
            row_settlement_mode="estimated_successful_rows",
        ),
        charge_authority="test.gw-authority",
        presentation=CommercialPresentation(
            venue_label="Hosted Gateway",
            billing_label="Test Gateway Billing",
        ),
    )
    gw_comp = ExecutionComposition(
        facts=CompositionFacts(
            edition="cloud",
            organization_id="org-test",
            funding=PlatformMetered(),
        ),
        provider=_StaticTargetsProvider(),
        credential_use_context=CredentialUseContext(cost_posture="platform_metered"),
        offerings=(gw_offer,),
    )

    a1 = project.add_blob(
        _make_wav_bytes(b"gw-a1"),
        filename="g1.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"kind": "audio", "duration_seconds": 60.0}
        ),
    )
    a2 = project.add_blob(
        _make_wav_bytes(b"gw-a2"),
        filename="g2.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"kind": "audio", "duration_seconds": 60.0}
        ),
    )
    asr_a, _, _ = _seed_audio_sheet(project, name="GW A", digest=a1)
    asr_b, _, _ = _seed_audio_sheet(project, name="GW B", digest=a1)
    asr_c = project.add_sheet("GW C")
    col_asr_c = project.add_column(asr_c, "recording", type="audio")
    project.add_rows(
        asr_c,
        [
            {"recording": media_cell(a1, mime="audio/wav", filename="g1.wav")},
            {"recording": media_cell(a2, mime="audio/wav", filename="g2.wav")},
        ],
        {"recording": col_asr_c},
    )

    run_ga, _ = _run_with_composition(
        project,
        _transcribe_request(asr_a, engine="parakeet-tdt", idempotency_key="gw-a"),
        router=router,
        composition=gw_comp,
    )
    assert len(gw_calls) == 1
    assert (
        run_attempt_receipts(project, run_ga)[-1]["settlement"]["charge_usd"] == "0.06"
    )

    # All-cached commercial run settles to "0" with 0 unmetered calls.
    run_gb, est_gb = _run_with_composition(
        project,
        _transcribe_request(asr_b, engine="parakeet-tdt", idempotency_key="gw-b"),
        router=router,
        composition=gw_comp,
    )
    assert est_gb["cached_rows"] == 1 and est_gb["live_rows"] == 0
    assert est_gb["cost"] == 0.0
    assert len(gw_calls) == 1
    receipt_gb = run_attempt_receipts(project, run_gb)[-1]["settlement"]
    assert receipt_gb["charge_usd"] == "0"
    assert receipt_gb["rated_calls"] == 1
    assert receipt_gb["unmetered_calls"] == 0
    assert "unsettleable" not in receipt_gb

    # Partial-cached commercial run (1 cached + 1 live = 1 min * $0.06 = $0.06).
    run_gc, est_gc = _run_with_composition(
        project,
        _transcribe_request(asr_c, engine="parakeet-tdt", idempotency_key="gw-c"),
        router=router,
        composition=gw_comp,
    )
    assert est_gc["cached_rows"] == 1 and est_gc["live_rows"] == 1
    assert len(gw_calls) == 2
    receipt_gc = run_attempt_receipts(project, run_gc)[-1]["settlement"]
    assert receipt_gc["charge_usd"] == "0.06"
    assert receipt_gc["rated_calls"] == 2
    assert receipt_gc["unmetered_calls"] == 0
    assert "unsettleable" not in receipt_gc

    # Direct custom meter_key="gpu_seconds" check via cache_hit_units + settle().
    gpu_basis = PricedCostBasis(
        pricing_key="test.offer.gpu_second",
        unit_rate="0.05",
        estimated_quantity="4",
        quantity_unit="gpu_second",
        terms_version="test.gpu.v1",
        quantity_rounding_mode="exact",
        quantity_rounding_decimal_places=None,
        meter_key="gpu_seconds",
        meter_units_per_quantity_unit="1",
        ceiling_mode="none",
        row_settlement_mode="all_metered",
        charge_authority="test.gpu-authority",
    )
    gpu_promises = compile_route_promises(
        RouteRowFacts(
            target_id=MODELS_GATEWAY_TARGET_ID,
            engine="parakeet-tdt",
            operator="frisket",
            egress_class="third_party_api",
            region="us",
            credential_source="platform",
            cost_posture="platform_metered",
        ),
        gpu_basis,
        cached_row_ids=[1],
    )

    class _FakeAdmission:
        promise_set = gpu_promises

    cached_units = cache_hit_units({"audio_seconds": 0.0}, _FakeAdmission())
    assert cached_units == {"audio_seconds": 0.0, "gpu_seconds": 0}
    basis_dict = {
        "kind": "priced",
        "pricing_key": gpu_basis.pricing_key,
        "unit_rate": gpu_basis.unit_rate,
        "estimated_quantity": gpu_basis.estimated_quantity,
        "quantity_unit": gpu_basis.quantity_unit,
        "terms_version": gpu_basis.terms_version,
        "quantity_rounding_mode": gpu_basis.quantity_rounding_mode,
        "quantity_rounding_decimal_places": gpu_basis.quantity_rounding_decimal_places,
        "meter_key": gpu_basis.meter_key,
        "meter_units_per_quantity_unit": gpu_basis.meter_units_per_quantity_unit,
        "ceiling_mode": gpu_basis.ceiling_mode,
        "row_settlement_mode": gpu_basis.row_settlement_mode,
        "charge_authority": gpu_basis.charge_authority,
    }
    all_cached_gpu = settle(
        cost_basis=basis_dict,
        metered_units=[cached_units],
        price_card_version="test.gpu.v1",
        terminal_status="completed",
        all_rows_cancelled=False,
    )
    assert all_cached_gpu["charge_usd"] == "0"
    assert all_cached_gpu["unmetered_calls"] == 0
    partial_gpu = settle(
        cost_basis=basis_dict,
        metered_units=[cached_units, {"audio_seconds": 10.0, "gpu_seconds": 4}],
        price_card_version="test.gpu.v1",
        terminal_status="completed",
        all_rows_cancelled=False,
    )
    assert partial_gpu["charge_usd"] == "0.2"
    assert partial_gpu["rated_calls"] == 2
    assert partial_gpu["unmetered_calls"] == 0


def test_direct_purge_prunes_cache_and_reclaims_orphan_disk_blobs_without_creating_sidecar(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    _patch_local_transcribe(monkeypatch, calls)

    # 1. Direct purge.py run prunes purged sheet's blob cache while preserving live sheets.
    project = make_project("direct-purge-cache")
    d1 = project.add_blob(
        _make_wav_bytes(b"purge-direct-1"),
        filename="d1.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"kind": "audio", "duration_seconds": 2.5}
        ),
    )
    d2 = project.add_blob(
        _make_wav_bytes(b"purge-direct-2"),
        filename="d2.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"kind": "audio", "duration_seconds": 2.5}
        ),
    )
    sheet_1, _, _ = _seed_audio_sheet(project, name="PurgeMe", digest=d1)
    sheet_2, _, _ = _seed_audio_sheet(project, name="KeepMe", digest=d2)
    router = ModelRouter(model_call_policy=LOCAL_PURCHASE_POLICY, cache_mode="replay")
    assert (
        run_action_spec(
            project,
            _transcribe_request(sheet_1, idempotency_key="dp-1"),
            project_id="direct-purge-cache",
            router=router,
        ).status
        == "completed"
    )
    assert (
        run_action_spec(
            project,
            _transcribe_request(sheet_2, idempotency_key="dp-2"),
            project_id="direct-purge-cache",
            router=router,
        ).status
        == "completed"
    )

    # Also plant an orphaned on-disk blob (no row in `blobs`) with a cache entry.
    orphan_disk = project.add_blob(
        _make_wav_bytes(b"orphan-disk-only"),
        filename="orphan-disk.wav",
        mime="audio/wav",
    )
    with write_transaction(project.db, what="remove blobs row leaving disk file"):
        project.db.execute("DELETE FROM blobs WHERE hash=?", (orphan_disk,))
    cache = BlobExtractionCache.for_project(project)
    assert cache is not None
    with cache:
        cache.put(
            build_extraction_cache_key(
                ExtractionTarget(
                    capability="transcribe",
                    engine="faster_whisper",
                    transport="local",
                ),
                blob_hash=orphan_disk,
                normalized_options={},
            ),
            {"text": "orphan disk", "segments": [], "language": "en", "duration": 1.0},
        )

    sidecar = cache_db_path(project)
    assert sidecar is not None and sidecar.exists()
    project_path = project.path
    project.close()

    # First verify reclaim_local_blobs prunes the orphaned on-disk blob's cache
    # row while retaining live sheets' cache rows (when project.cache.db exists).
    maint = Project(project_path, maintenance=True)
    try:
        reclaim_local_blobs(maint, dry_run=False)
        conn = sqlite3.connect(sidecar)
        try:
            after_reclaim = {
                str(r[0])
                for r in conn.execute(
                    "SELECT DISTINCT blob_hash FROM blob_extraction_cache"
                ).fetchall()
            }
        finally:
            conn.close()
        assert orphan_disk not in after_reclaim
        assert {d1, d2} <= after_reclaim

        # Direct purge.py run removes project.cache.db completely and does NOT
        # recreate an empty sidecar during _erase_source_blobs / reclaim_local_blobs.
        purge_id = begin_purge(maint, sheet_ids=[sheet_1])
        finish_purge(maint, purge_id)
        assert not sidecar.exists()
    finally:
        maint.close()

    # 2. Direct purge on a project WITHOUT project.cache.db MUST NOT create an empty sidecar.
    no_cache_project = make_project("purge-no-sidecar")
    d3 = no_cache_project.add_blob(
        _make_wav_bytes(b"no-sidecar-blob"),
        filename="d3.wav",
        mime="audio/wav",
    )
    sheet_3, _, _ = _seed_audio_sheet(no_cache_project, name="S3", digest=d3)
    no_cache_sidecar = cache_db_path(no_cache_project)
    assert no_cache_sidecar is not None and not no_cache_sidecar.exists()
    no_cache_path = no_cache_project.path
    no_cache_project.close()

    maint2 = Project(no_cache_path, maintenance=True)
    try:
        purge_id_2 = begin_purge(maint2, sheet_ids=[sheet_3])
        finish_purge(maint2, purge_id_2)
    finally:
        maint2.close()
    assert not no_cache_sidecar.exists()


def test_symlink_refusal_on_cache_db_and_wal_shm_sidecars(
    make_project, tmp_path: Path
) -> None:
    project = make_project("symlink-refusal")
    digest = project.add_blob(
        _make_wav_bytes(b"sym-audio"),
        filename="sym.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"kind": "audio", "duration_seconds": 2.5}
        ),
    )
    sheet_id, _, row_id = _seed_audio_sheet(project, name="S", digest=digest)
    sidecar = cache_db_path(project)
    assert sidecar is not None

    target = ExtractionTarget(
        capability="transcribe", engine="faster_whisper", transport="local"
    )
    key = build_extraction_cache_key(
        target, blob_hash=digest, normalized_options={"language": None}
    )
    payload = {"text": "secret", "segments": [], "language": "en", "duration": 2.5}
    bound = typed_action_for_request(
        _transcribe_request(sheet_id, idempotency_key="sym-req")
    )
    plan = build_typed_map_rows_plan(project, bound, _admit_output_targets=False)

    # 1. Symlinked project.cache.db -> refused on put, get, delete, and probe.
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"sentinel-untouched")
    sidecar.symlink_to(outside)

    with BlobExtractionCache(sidecar) as cache:
        cache.put(key, payload)
        assert cache.get(key) is None
        assert cache.delete_for_blobs([digest]) == 0
    cached_ids, live_ids = probe_extraction_cache_rows(
        project, target, spec=plan.spec_dict(), program=plan.program, row_ids=[row_id]
    )
    assert cached_ids == frozenset()
    assert live_ids == frozenset({row_id})
    assert outside.read_bytes() == b"sentinel-untouched"
    sidecar.unlink()

    # 2. Normal cache write uses WAL and survives multiple puts, then symlinked
    #    -wal and -shm sidecars are both refused cleanly.
    with BlobExtractionCache(sidecar) as cache:
        cache.put(key, payload)
        cache.put(key, {**payload, "text": "updated"})
        assert cache.get(key) is not None

    wal_path = Path(f"{sidecar}-wal")
    shm_path = Path(f"{sidecar}-shm")
    wal_path.unlink(missing_ok=True)
    shm_path.unlink(missing_ok=True)
    outside_wal = tmp_path / "outside.wal"
    outside_wal.write_bytes(b"wal-sentinel")
    wal_path.symlink_to(outside_wal)

    with BlobExtractionCache(sidecar) as cache:
        assert cache.get(key) is None
        cache.put(key, payload)
        assert cache.delete_for_blobs([digest]) == 0
    cached_ids, live_ids = probe_extraction_cache_rows(
        project, target, spec=plan.spec_dict(), program=plan.program, row_ids=[row_id]
    )
    assert cached_ids == frozenset()
    assert live_ids == frozenset({row_id})
    assert outside_wal.read_bytes() == b"wal-sentinel"
    wal_path.unlink()

    outside_shm = tmp_path / "outside.shm"
    outside_shm.write_bytes(b"shm-sentinel")
    shm_path.symlink_to(outside_shm)
    with BlobExtractionCache(sidecar) as cache:
        assert cache.get(key) is None
    assert outside_shm.read_bytes() == b"shm-sentinel"
    shm_path.unlink()


def test_mid_run_toctou_partial_hit_transcriber_and_admit_violation_recording(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify:
    1. admit_routed(..., program=None) resolves program defensively and records
       a route_violations row when cached entries vanish before admission.
    2. Partial-hit run (1 cached + 1 live row) where the cached row vanishes
       mid-run halts with promise_violation without calling the paid API.
    3. Direct _BoundTranscriber._transcribe mid-run TOCTOU halts cleanly with
       0 paid API calls."""
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
            "text": "Paid Whisper",
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

    project = make_project("toctou-partial-asr")
    d_cached = project.add_blob(
        _make_wav_bytes(b"toctou-cached"),
        filename="cached.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"kind": "audio", "duration_seconds": 10.0}
        ),
    )
    d_live = project.add_blob(
        _make_wav_bytes(b"toctou-live"),
        filename="live.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"kind": "audio", "duration_seconds": 10.0}
        ),
    )
    sheet_seed, _, _ = _seed_audio_sheet(project, name="Seed", digest=d_cached)
    sheet_mix = project.add_sheet("Mixed")
    col_mix = project.add_column(sheet_mix, "recording", type="audio")
    [row_cached, row_live] = project.add_rows(
        sheet_mix,
        [
            {"recording": media_cell(d_cached, mime="audio/wav", filename="c.wav")},
            {"recording": media_cell(d_live, mime="audio/wav", filename="l.wav")},
        ],
        {"recording": col_mix},
    )

    router = ModelRouter(model_call_policy=LOCAL_PURCHASE_POLICY, cache_mode="replay")
    deps = ExecutorDeps(
        consent_coverage=ConsentCoverage(instance_principal(project), Decimal("0")),
    )

    # Seed d_cached via confirmed Sheet Seed.
    req_seed = _transcribe_request(
        sheet_seed, engine="openai/whisper-1", idempotency_key="toctou-seed"
    )
    _, _, est_seed = _estimate_typed_action(
        project, req_seed, router=router, threshold_usd=Decimal("0")
    )
    res_seed = run_action_spec(
        project,
        {**req_seed, "confirmation": est_seed["promise_set_hash"]},
        project_id="toctou-partial-asr",
        router=router,
        deps=deps,
    )
    assert res_seed.status == "completed", res_seed.errors
    assert len(paid_calls) == 1

    # Prepare partial-hit run on Sheet Mixed (row_cached=cached, row_live=live).
    req_mix = _transcribe_request(
        sheet_mix, engine="openai/whisper-1", idempotency_key="toctou-mix"
    )
    runner_mix, plan_mix, est_mix = _estimate_typed_action(
        project, req_mix, router=router, threshold_usd=Decimal("0")
    )
    assert est_mix["cached_rows"] == 1
    assert est_mix["live_rows"] == 1
    spec_mix = dict(plan_mix.spec_dict())
    if est_mix.get("promise_set_hash"):
        spec_mix["consented_promise_set_hash"] = est_mix["promise_set_hash"]
    prepared = runner_mix.prepare_run(
        spec_mix, program=plan_mix.program, confirmed=True
    )
    assert prepared.run_id is not None

    sidecar = cache_db_path(project)
    assert sidecar is not None and sidecar.exists()
    backup = sidecar.read_bytes()

    # 1. Delete cache before admit_routed -> promise_violation + route_violations row!
    sidecar.unlink()
    composition = open_execution_composition(
        project, router, ExecutionCompositionContext.direct()
    )
    coverage = ConsentCoverage(instance_principal(project), Decimal("0"))
    with pytest.raises(ActionProgramInvocationHalt) as admit_halted:
        admit_routed(
            project,
            prepared.run_id,
            spec_mix,
            composition=composition,
            program=plan_mix.program,
            scope=(row_cached, row_live),
            consent_coverage=coverage,
        )
    assert admit_halted.value.code == "promise_violation"
    violations = project.db.execute(
        "SELECT observed_json FROM route_violations"
    ).fetchall()
    assert len(violations) == 1
    assert json.loads(violations[0]["observed_json"])[
        "extraction_cache_missing_rows"
    ] == [row_cached]

    # Also verify probe_extraction_cache_rows works defensively when program=None.
    sidecar.write_bytes(backup)
    cached_no_prog, live_no_prog = probe_extraction_cache_rows(
        project,
        ExtractionTarget(
            capability="transcribe",
            engine="openai/whisper-1",
            transport="remote",
        ),
        spec=spec_mix,
        program=None,
        row_ids=[row_cached, row_live],
    )
    assert cached_no_prog == frozenset({row_cached})
    assert live_no_prog == frozenset({row_live})

    # 2 & 3. Restore cache so admission succeeds on the partial-hit scope,
    #        then delete cache mid-run before executing row_cached in _BoundTranscriber!
    sidecar.write_bytes(backup)
    commitment = runner_mix.authority.mint(
        program=plan_mix.program,
        spec=spec_mix,
        run_id=prepared.run_id,
        scope=(row_cached, row_live),
    )
    sidecar.unlink()

    cell_cached = project.get_values(sheet_mix, col_mix, row_ids=[row_cached])[
        row_cached
    ]
    ctx = ActionContext(
        project=project,
        extras={
            ATTEMPT_EXTRA: commitment,
            "router": router,
            "cancelled": lambda: False,
        },
    )
    options = TranscriptionOptions()
    transcriber = AdmittedTranscriber(
        ctx,
        engine="openai/whisper-1",
        options=options.normalize("openai/whisper-1"),
    )

    async def _run_transcribe_toctou() -> None:
        await transcriber.start(expected_rows=2)
        try:
            row_obj = Row({"recording": cell_cached})
            bound = transcriber.bind_row(
                row_obj,
                sheet_id=sheet_mix,
                row_id=row_cached,
                sources={
                    "recording": {
                        "column_id": col_mix,
                        "column_type": "audio",
                        "value": cell_cached,
                    }
                },
                ctx=ctx,
            )
            await bound.transcribe(row_obj, ColumnRef("recording"), options=options)
        finally:
            await transcriber.aclose()

    with pytest.raises(ActionProgramInvocationHalt) as mid_halted:
        asyncio.run(_run_transcribe_toctou())
    assert mid_halted.value.code == "promise_violation"
    assert len(paid_calls) == 1


def test_searchable_pdf_cache_hit_end_to_end(
    make_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sheet B with searchable_pdf=True hits Sheet A's cache with 0 engine calls
    and produces its own valid staged searchable PDF blob."""
    calls: list[dict[str, Any]] = []
    _patch_local_ocr(monkeypatch, calls)

    project = make_project("searchable-pdf-cache")
    digest = project.add_blob(
        _make_pdf_bytes(page_count=2),
        filename="scan.pdf",
        mime="application/pdf",
        metadata=owned_media_metadata_document(probe={"kind": "pdf", "pages": 2}),
    )
    sheet_a, _, row_a = _seed_pdf_sheet(project, name="Sheet A", digest=digest)
    sheet_b, _, row_b = _seed_pdf_sheet(project, name="Sheet B", digest=digest)
    router = ModelRouter(model_call_policy=LOCAL_PURCHASE_POLICY, cache_mode="replay")

    first = run_action_spec(
        project,
        _ocr_request(sheet_a, searchable_pdf=True, idempotency_key="spdf-a"),
        project_id="searchable-pdf-cache",
        router=router,
    )
    assert first.status == "completed", first.errors
    assert len(calls) == 2

    second = run_action_spec(
        project,
        _ocr_request(sheet_b, searchable_pdf=True, idempotency_key="spdf-b"),
        project_id="searchable-pdf-cache",
        router=router,
    )
    assert second.status == "completed", second.errors
    assert len(calls) == 2

    col_pdf_a = next(
        c["id"] for c in project.columns(sheet_a) if c["name"] == "ocr_pdf"
    )
    col_pdf_b = next(
        c["id"] for c in project.columns(sheet_b) if c["name"] == "ocr_pdf"
    )
    val_a = project.get_values(sheet_a, col_pdf_a, row_ids=[row_a])[row_a]
    val_b = project.get_values(sheet_b, col_pdf_b, row_ids=[row_b])[row_b]
    assert isinstance(val_a, dict) and val_a.get("blob")
    assert isinstance(val_b, dict) and val_b.get("blob")
    assert val_b["mime"] == "application/pdf"
    with project.materialize_blob(val_b["blob"]) as pdf_path:
        reader = PdfReader(pdf_path)
        assert len(reader.pages) == 2
        extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
        assert "Page text 1" in extracted
        assert "Page text 2" in extracted
