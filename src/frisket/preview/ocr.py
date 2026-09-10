"""Read-only OCR comparison preview service.

This module deliberately sits outside the durable ``media.ocr`` action path.
It resolves one blob-backed PDF cell, runs selected pages through two OCR
engines, and returns request-local comparison artifacts without writing runs,
results, receipts, ops, rows, columns, evidence, or project blobs.

FREE ENGINES ONLY. Billable → run; not billable → preview — one predicate,
no preview taxonomy. Writing nothing is exactly why a billable engine cannot
live here: a comparison that spends money at a provider endpoint already IS a
run, and running it on this surface produced no receipt, no attempt, no egress
record and no spend line. So ``datalab`` and every ``provider/model`` VLM id
are refused and pointed at the cost-gated ``media.ocr`` action. The refusal is
structural, not a flag: there is no ``allow_remote`` and no ``router``
parameter, so a billable venue has no credentials in scope. The sidecar http
client stays — ``paddleocr-vl``/``surya2``/``dots.mocr``/``glm-ocr`` run on operator-owned
or explicitly configured model infrastructure.
"""

from __future__ import annotations

import tempfile
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from frisket.preview.common import (
    BillablePreviewDispatch,
    ComparePreviewError,
    billable_engine_error,
    canonical_engine,
    engine_descriptor,
    str_or_none,
)
from frisket.contracts.action import OCR_SYMBOLIC_ENGINES
from frisket.contracts.actions.schemas._engines import (
    OCR_ENGINE_TABLE,
    is_billable_engine,
)
from frisket.ops.base import OpContext
from frisket.ops.ocr_engines import (
    DEFAULT_DPI,
    ENGINE_ALIASES,
    LIGHT_ENGINE,
    rapidocr_execution_scope,
    SIDECAR_ENGINES,
    OcrEngines,
)
from frisket.redaction import redact_text, safe_error
from frisket.engine.store import Project
from frisket.engine.store.blob_backend import BlobNotFoundError
from frisket.engine.store.media_blobs import MediaBlobStore

SCHEMA_VERSION = "frisket.ocr_compare_preview.v1"
MAX_PREVIEW_PAGES = 10


class OcrComparePreviewError(ComparePreviewError):
    """Validation/runtime error that should be returned as a preview 400."""


@dataclass(frozen=True)
class OcrComparePreviewRequest:
    sheet_id: int
    row_id: int
    input_column: str
    pages: list[int]
    engines: list[str]
    language: str | None = None
    dpi: int = DEFAULT_DPI


@dataclass(frozen=True)
class _EnginesOutcome:
    results: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    page_count: int | None


@dataclass(frozen=True)
class OcrCompareSource:
    sheet_id: int
    row_id: int
    column_id: int
    column_name: str
    column_type: str
    blob_hash: str
    filename: str | None
    mime: str | None
    size: int | None
    page_count: int | None
    path: Path | None
    media: dict[str, Any]


@dataclass(frozen=True)
class RenderedPreviewPage:
    page: int
    path: Path
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class RenderedPreviewDocument:
    pages: list[RenderedPreviewPage]
    page_count: int | None = None


@dataclass(frozen=True)
class OcrEnginePreviewOutput:
    pages: list[dict[str, Any]]


async def compare_ocr_preview(
    project: Project,
    request: OcrComparePreviewRequest | Mapping[str, Any],
) -> dict[str, Any]:
    """Run a transient two-engine OCR preview for selected PDF pages."""

    req = _coerce_request(request)
    source = resolve_ocr_compare_source(project, req)
    if source.page_count is not None:
        _reject_pages_past_page_count(req.pages, source.page_count)

    canonical_engines = [_canonical_engine(engine) for engine in req.engines]
    engine_descriptors = [
        engine_descriptor(OCR_ENGINE_TABLE, engine) for engine in canonical_engines
    ]
    try:
        with project.materialize_blob(source.blob_hash) as path:
            materialized = replace(source, path=Path(path))
            if not _is_pdf_source(
                materialized.path,
                filename=materialized.filename,
                mime=materialized.mime,
            ):
                raise OcrComparePreviewError(
                    "invalid_input_ref",
                    "OCR comparison preview supports PDF blob cells only",
                    field="input_column",
                )
            outcome = await _render_and_run_engines(
                project,
                materialized,
                req.pages,
                canonical_engines,
                engine_descriptors,
                language=req.language,
                dpi=req.dpi,
            )
    except BlobNotFoundError as exc:
        raise OcrComparePreviewError(
            "invalid_input_ref",
            "OCR preview input blob is missing from project storage",
            field="input_column",
            details={"blob_hash": source.blob_hash},
        ) from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "sheet_id": source.sheet_id,
            "row_id": source.row_id,
            "column_id": source.column_id,
            "column_name": source.column_name,
            "filename": source.filename,
            "mime": source.mime,
            "blob_hash": source.blob_hash,
            "size": source.size,
            "page_count": outcome.page_count,
        },
        "pages": list(req.pages),
        "engines": engine_descriptors,
        "results": outcome.results,
        "warnings": outcome.warnings,
        "errors": outcome.errors,
    }


async def _render_and_run_engines(
    project: Project,
    source: OcrCompareSource,
    pages: list[int],
    canonical_engines: list[str],
    engine_descriptors: list[dict[str, Any]],
    *,
    language: str | None,
    dpi: int,
) -> _EnginesOutcome:
    """Rasterize the selected pages and run each engine over them.

    Source-agnostic: works identically for a project blob cell and for scratch
    PDF bytes. Writes nothing durable — only a request-local scratch directory.
    """

    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="frisket-ocr-compare-") as tmp:
        rendered = await render_selected_pdf_pages(
            source,
            pages,
            dpi=dpi,
            scratch=Path(tmp),
        )
        page_count = (
            rendered.page_count
            if rendered.page_count is not None
            else source.page_count
        )
        if page_count is not None:
            _reject_pages_past_page_count(pages, page_count)
        rendered_by_page = {page.page: page for page in rendered.pages}
        missing_rendered = [page for page in pages if page not in rendered_by_page]
        if missing_rendered:
            raise OcrComparePreviewError(
                "invalid_page_ref",
                "OCR preview pages must exist in the selected PDF",
                field="pages",
                details={"missing_pages": missing_rendered, "page_count": page_count},
            )

        for engine, descriptor in zip(
            canonical_engines, engine_descriptors, strict=True
        ):
            started = time.perf_counter()
            try:
                engine_output = await run_ocr_engine_preview(
                    engine,
                    rendered.pages,
                    project=project,
                    language=language,
                    scratch=Path(tmp),
                )
                engine_pages = engine_output.pages
            except BillablePreviewDispatch:
                # Never degraded into a per-engine error: a billable engine
                # reaching dispatch means the refusal in _coerce_* has a hole.
                raise
            except Exception as exc:  # noqa: BLE001 - preview reports engine failure
                safe = safe_error(
                    "ocr_preview_engine_failed",
                    exc,
                    max_chars=300,
                )
                errors.append(
                    {
                        "code": safe.code,
                        "engine": engine,
                        "message": safe.detail,
                    }
                )
                engine_pages = []

            runtime_ms = int((time.perf_counter() - started) * 1000)
            if len(engine_pages) != len(rendered.pages):
                warnings.append(
                    {
                        "code": "ocr_preview_page_result_mismatch",
                        "engine": engine,
                        "message": (
                            "OCR engine returned a different page count than requested"
                        ),
                        "details": {
                            "requested_pages": len(rendered.pages),
                            "returned_pages": len(engine_pages),
                        },
                    }
                )
            for index, rendered_page in enumerate(rendered.pages):
                raw_page = engine_pages[index] if index < len(engine_pages) else {}
                page_errors = _page_messages(raw_page.get("errors"))
                if raw_page.get("error"):
                    page_errors.extend(_page_messages(raw_page["error"]))
                results.append(
                    {
                        "page": rendered_page.page,
                        "engine": descriptor["id"],
                        "text": str(raw_page.get("text") or ""),
                        "blocks": normalize_ocr_blocks(
                            raw_page.get("blocks"),
                            width=rendered_page.width,
                            height=rendered_page.height,
                        ),
                        "warnings": _page_messages(raw_page.get("warnings")),
                        "errors": page_errors,
                        "runtime_ms": runtime_ms,
                    }
                )

    return _EnginesOutcome(
        results=results,
        warnings=warnings,
        errors=errors,
        page_count=page_count,
    )


def resolve_ocr_compare_source(
    project: Project, request: OcrComparePreviewRequest
) -> OcrCompareSource:
    sheet = project.db.execute(
        "SELECT id FROM sheets WHERE id=? AND hidden=0",
        (request.sheet_id,),
    ).fetchone()
    if sheet is None:
        raise OcrComparePreviewError(
            "invalid_input_ref",
            "OCR preview sheet_id does not identify a visible sheet",
            field="sheet_id",
        )
    row = project.db.execute(
        "SELECT id FROM rows WHERE id=? AND sheet_id=? AND hidden=0",
        (request.row_id, request.sheet_id),
    ).fetchone()
    if row is None:
        raise OcrComparePreviewError(
            "invalid_input_ref",
            "OCR preview row_id must belong to the target sheet",
            field="row_id",
            details={"row_id": request.row_id},
        )
    column = project.db.execute(
        "SELECT * FROM columns WHERE sheet_id=? AND name=? AND hidden=0",
        (request.sheet_id, request.input_column),
    ).fetchone()
    if column is None:
        raise OcrComparePreviewError(
            "invalid_input_ref",
            "OCR preview input column does not exist on the sheet",
            field="input_column",
            details={"column": request.input_column},
        )
    if column["type"] not in {"file", "image"}:
        raise OcrComparePreviewError(
            "invalid_input_ref",
            "OCR preview input column must be a file or image column",
            field="input_column",
            details={"column": request.input_column, "type": column["type"]},
        )

    values = project.get_values(
        request.sheet_id,
        int(column["id"]),
        row_ids=[request.row_id],
    )
    value = values.get(request.row_id)
    if not isinstance(value, dict) or not isinstance(value.get("blob"), str):
        raise OcrComparePreviewError(
            "invalid_input_ref",
            "OCR preview requires a blob-backed PDF cell",
            field="input_column",
            details={"row_id": request.row_id, "column": request.input_column},
        )

    digest = value["blob"]
    blob = project.db.execute(
        "SELECT hash, filename, mime, size FROM blobs WHERE hash=?",
        (digest,),
    ).fetchone()
    if blob is None:
        raise OcrComparePreviewError(
            "invalid_input_ref",
            "OCR preview input blob row is missing from the project",
            field="input_column",
            details={"row_id": request.row_id, "blob_hash": digest},
        )
    metadata = MediaBlobStore(project).probe_metadata(digest)
    filename = _str_or_none(value.get("filename")) or _str_or_none(blob["filename"])
    mime = _str_or_none(value.get("mime")) or _str_or_none(blob["mime"])
    media = dict(value)
    media.setdefault("filename", filename)
    media.setdefault("mime", mime)
    page_count = _positive_int(metadata.get("pages") or metadata.get("page_count"))
    return OcrCompareSource(
        sheet_id=request.sheet_id,
        row_id=request.row_id,
        column_id=int(column["id"]),
        column_name=str(column["name"]),
        column_type=str(column["type"]),
        blob_hash=digest,
        filename=filename,
        mime=mime,
        size=int(blob["size"]) if blob["size"] is not None else None,
        page_count=page_count,
        path=None,
        media=media,
    )


async def render_selected_pdf_pages(
    source: OcrCompareSource,
    pages: list[int],
    *,
    dpi: int,
    scratch: Path,
    max_pdf_pages: int | None = None,
) -> RenderedPreviewDocument:
    """Rasterize a PDF and return only the selected 1-based page numbers."""

    if source.path is None:
        raise OcrComparePreviewError(
            "invalid_input_ref", "OCR comparison source is not materialized"
        )
    recipe = OcrEngines()
    page_count = source.page_count
    is_pdf = recipe._is_pdf(source.path, source.media)
    if is_pdf:
        if page_count is None:
            page_count = _positive_int(
                recipe._pdf_page_count(source.path, source.blob_hash)
            )
        from frisket.execution.provider import enforce_pdf_page_limit

        enforce_pdf_page_limit(max_pdf_pages, page_count)
        if page_count is not None:
            _reject_pages_past_page_count(pages, page_count)
    page_paths = await recipe._page_images(
        source.path,
        source.media,
        {"dpi": dpi, **({"_selected_pages": pages} if is_pdf else {})},
        scratch,
    )
    if page_count is None and not is_pdf:
        page_count = len(page_paths)
    if not is_pdf:
        _reject_pages_past_page_count(pages, page_count)
    rendered = []
    for page, page_path in zip(pages, page_paths, strict=True):
        width, height = _image_size(page_path)
        rendered.append(
            RenderedPreviewPage(
                page=page,
                path=page_path,
                width=width,
                height=height,
            )
        )
    return RenderedPreviewDocument(pages=rendered, page_count=page_count)


async def run_ocr_engine_preview(
    engine: str,
    pages: list[RenderedPreviewPage],
    *,
    project: Project,
    language: str | None,
    scratch: Path,
) -> OcrEnginePreviewOutput:
    """Run one OCR engine against already rendered page images through the
    recipe's public dispatch (``OcrEngines.run_engine_on_pages`` — the same
    path the durable action takes; no shadow dispatch table).

    The preview supplies only request-local context, and deliberately no
    model router: the remote VLM ids that used to ride it have no credentials
    in scope here at all. The http client stays, because it serves the
    SIDECAR engines (``paddleocr-vl``, ``surya2``, ``dots.mocr``, ``glm-ocr``) — those run on
    operator's own box and bill nothing.
    """

    recipe = OcrEngines()
    page_paths = [page.path for page in pages]
    usage: dict[str, Any] = {"calls": 0, "in": 0, "out": 0, "cost": 0.0}
    if _is_remote_engine(engine):
        # The effect-site fence. ``_coerce_request``
        # already refused this with a 400; reaching here means that refusal has
        # a hole, which is a bug to surface loudly, never a per-engine 200.
        # Note this uses ``_is_remote_engine`` (which wraps
        # ``is_billable_engine``), not the old bare ``"/" in engine``
        # heuristic, which misclassified symbolic hosted engines (datalab)
        # as free.
        raise BillablePreviewDispatch(
            f"OCR compare tried to dispatch billable engine '{engine}'; "
            "billable engines belong to the cost-gated media.ocr action"
        )
    async with AsyncExitStack() as stack:
        if engine == LIGHT_ENGINE:
            recipe.pool = await stack.enter_async_context(
                rapidocr_execution_scope(expected_rows=1, language=language)
            )
        http = None
        if engine in SIDECAR_ENGINES:
            import httpx

            http = await stack.enter_async_context(httpx.AsyncClient())
        output_pages = await recipe.run_engine_on_pages(
            engine,
            page_paths,
            OpContext(project=project, http=http, extras={}),
            usage=usage,
            language=language,
            scratch=scratch,
        )
    return OcrEnginePreviewOutput(pages=output_pages)


def normalize_ocr_blocks(
    blocks: Any,
    *,
    width: int | None,
    height: int | None,
) -> list[dict[str, Any]]:
    if not isinstance(blocks, list):
        return []
    normalized: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        out: dict[str, Any] = {"text": str(block.get("text") or "")}
        bbox = _normalize_bbox(block.get("bbox"), width=width, height=height)
        if bbox is not None:
            out["bbox"] = bbox
        if "score" in block:
            try:
                out["score"] = float(block["score"])
            except (TypeError, ValueError):
                pass
        out["raw"] = block
        if out["text"] or bbox is not None:
            normalized.append(out)
    return normalized


def _coerce_request(
    request: OcrComparePreviewRequest | Mapping[str, Any],
) -> OcrComparePreviewRequest:
    if isinstance(request, OcrComparePreviewRequest):
        req = request
    else:
        req = OcrComparePreviewRequest(
            sheet_id=_strict_positive_int(request.get("sheet_id"), "sheet_id"),
            row_id=_strict_positive_int(request.get("row_id"), "row_id"),
            input_column=_nonblank_string(request.get("input_column"), "input_column"),
            pages=_coerce_pages(request.get("pages")),
            engines=_coerce_engines(request.get("engines")),
            language=_optional_nonblank_string(request.get("language"), "language"),
            dpi=_coerce_dpi(request.get("dpi", DEFAULT_DPI)),
        )
    canonical = [_canonical_engine(engine) for engine in req.engines]
    if len(canonical) != len(set(canonical)):
        raise OcrComparePreviewError(
            "invalid_ocr_engine",
            "OCR preview requires two distinct OCR engines",
            field="engines",
            details={"engines": req.engines},
        )
    _refuse_billable_engines(canonical)
    return req


def _refuse_billable_engines(canonical_engines: list[str]) -> None:
    """Billable → run. Refuse before any dispatch could reach a provider."""
    for engine in canonical_engines:
        if _is_remote_engine(engine):
            raise billable_engine_error(
                engine,
                error=OcrComparePreviewError,
                action_kind="media.ocr",
            )


def _strict_positive_int(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise OcrComparePreviewError(
            "invalid_input_ref",
            f"OCR preview {field} must be a positive integer",
            field=field,
        )
    return value


def _nonblank_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OcrComparePreviewError(
            "invalid_params",
            f"OCR preview {field} must be a non-empty string",
            field=field,
        )
    return value.strip()


def _optional_nonblank_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _nonblank_string(value, field)


def _coerce_dpi(value: Any) -> int:
    if type(value) is not int or value < 50 or value > 600:
        raise OcrComparePreviewError(
            "invalid_params",
            "OCR preview dpi must be an integer between 50 and 600",
            field="dpi",
        )
    return value


def _coerce_pages(value: Any) -> list[int]:
    if not isinstance(value, list) or not value:
        raise OcrComparePreviewError(
            "invalid_page_ref",
            "OCR preview requires 1-10 explicit page numbers",
            field="pages",
        )
    if len(value) > MAX_PREVIEW_PAGES:
        raise OcrComparePreviewError(
            "invalid_page_ref",
            "OCR preview accepts at most 10 pages",
            field="pages",
            details={"max_pages": MAX_PREVIEW_PAGES},
        )
    pages: list[int] = []
    for index, item in enumerate(value):
        if type(item) is not int or item <= 0:
            raise OcrComparePreviewError(
                "invalid_page_ref",
                "OCR preview pages must be positive integers",
                field=f"pages[{index}]",
            )
        pages.append(item)
    if len(pages) != len(set(pages)):
        raise OcrComparePreviewError(
            "invalid_page_ref",
            "OCR preview pages must not contain duplicates",
            field="pages",
        )
    return pages


def _coerce_engines(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) != 2:
        raise OcrComparePreviewError(
            "invalid_ocr_engine",
            "OCR preview requires exactly two OCR engines",
            field="engines",
        )
    engines = [
        _nonblank_string(item, f"engines[{index}]") for index, item in enumerate(value)
    ]
    for index, engine in enumerate(engines):
        _validate_engine(engine, field=f"engines[{index}]")
    return engines


def _validate_engine(engine: str, *, field: str) -> None:
    if "/" in engine:
        provider, model = engine.split("/", 1)
        if provider.strip() and model.strip():
            return
    elif engine in OCR_SYMBOLIC_ENGINES:
        return
    raise OcrComparePreviewError(
        "invalid_ocr_engine",
        "Unknown OCR preview engine",
        field=field,
        details={"engine": engine},
    )


def _canonical_engine(engine: str) -> str:
    return canonical_engine(engine, ENGINE_ALIASES)


def _is_remote_engine(engine: str) -> bool:
    # Billable engines are refused outright on this surface: any
    # provider/model id, plus table engines declared billable (datalab is
    # billable even though its id has no "/").
    return is_billable_engine(OCR_ENGINE_TABLE, engine)


def _is_pdf_source(path: Path, *, filename: str | None, mime: str | None) -> bool:
    if mime and "pdf" in mime.lower():
        return True
    if filename and Path(filename).suffix.lower() == ".pdf":
        return True
    try:
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def _positive_int(value: Any) -> int | None:
    if type(value) is int and value > 0:
        return value
    return None


def _reject_pages_past_page_count(pages: list[int], page_count: int) -> None:
    missing = [page for page in pages if page > page_count]
    if missing:
        raise OcrComparePreviewError(
            "invalid_page_ref",
            "OCR preview pages must exist in the selected PDF",
            field="pages",
            details={"missing_pages": missing, "page_count": page_count},
        )


def _image_size(path: Path) -> tuple[int | None, int | None]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:  # noqa: BLE001 - geometry is optional in preview
        return None, None


def _normalize_bbox(
    bbox: Any,
    *,
    width: int | None,
    height: int | None,
) -> dict[str, Any] | None:
    coords = _bbox_coords(bbox)
    if coords is None:
        return None
    x0, y0, x1, y1 = coords
    if width and height and max(abs(x0), abs(y0), abs(x1), abs(y1)) > 1.0:
        x0, x1 = x0 / width, x1 / width
        y0, y1 = y0 / height, y1 / height
    if not all(0.0 <= value <= 1.0 for value in (x0, y0, x1, y1)):
        return None
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return {
        "space": "page_normalized",
        "x0": round(x0, 6),
        "y0": round(y0, 6),
        "x1": round(x1, 6),
        "y1": round(y1, 6),
    }


def _bbox_coords(bbox: Any) -> tuple[float, float, float, float] | None:
    if isinstance(bbox, dict):
        values = [bbox.get(key) for key in ("x0", "y0", "x1", "y1")]
        if all(isinstance(value, int | float) for value in values):
            return tuple(float(value) for value in values)  # type: ignore[return-value]
        return None
    if not isinstance(bbox, list):
        return None
    if len(bbox) == 4 and all(isinstance(value, int | float) for value in bbox):
        return tuple(float(value) for value in bbox)  # type: ignore[return-value]
    points: list[tuple[float, float]] = []
    for item in bbox:
        if (
            isinstance(item, list | tuple)
            and len(item) >= 2
            and isinstance(item[0], int | float)
            and isinstance(item[1], int | float)
        ):
            points.append((float(item[0]), float(item[1])))
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _page_messages(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [
            redact_text(item, max_chars=300)  # type: ignore[arg-type]
            for item in value
            if item is not None
        ]
    return [redact_text(value, max_chars=300)]  # type: ignore[arg-type]


def _str_or_none(value: Any) -> str | None:
    return str_or_none(value, strip=False)
