"""OCR dispatch, rendering, and result projection."""

from __future__ import annotations

import hashlib
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from frisket.ai.llm.types import provider_from_model_id
from frisket.ai.models.metadata import ModelCallMeta
from frisket.contracts.actions.schemas._engines import (
    OCR_ENGINE_TABLE,
    engine_ids,
    execution_alias_map,
)
from frisket.engine.sandbox import fence
from frisket.engine.sandbox.shim import SandboxPolicy, run_sandboxed
from frisket.execution.targets import CAPABILITY_OCR
from frisket.ops.base import OpContext
from frisket.ops.media_metadata import image_dimensions as _image_dimensions
from frisket.ops import ocr_engines_hosted as hosted
from frisket.ops import ocr_engines_local as local
from frisket.ops import ocr_engines_sidecar as sidecar

logger = logging.getLogger("frisket.executor")

LIGHT_ENGINE = local.LIGHT_ENGINE
LOCAL_ENGINES = set(engine_ids(OCR_ENGINE_TABLE, tier="local"))
SIDECAR_ENGINES = set(engine_ids(OCR_ENGINE_TABLE, tier="sidecar"))
DATALAB_ENGINE = hosted.DATALAB_ENGINE
MINIMAX_M3_ENGINE = hosted.MINIMAX_M3_ENGINE
MINIMAX_M3_MAX_IMAGE_EDGE = hosted.MINIMAX_M3_MAX_IMAGE_EDGE
ENGINE_ALIASES = execution_alias_map(OCR_ENGINE_TABLE)
RUN_SCOPED_ENGINES = frozenset(
    entry.id for entry in OCR_ENGINE_TABLE if entry.run_scoped
)
DEFAULT_DPI = 200
image_dimensions = _image_dimensions

OcrCancelled = local.OcrCancelled
RAPIDOCR_MODELS_NOT_PROVISIONED = local.RAPIDOCR_MODELS_NOT_PROVISIONED
rapidocr_available = local.rapidocr_available
rapidocr_models_present = local.rapidocr_models_present
rapidocr_shared_model_cache_dir = local.rapidocr_shared_model_cache_dir
rapidocr_execution_scope = local.rapidocr_execution_scope
tesseract_available = local.tesseract_available
_tesseract_language_code = local._tesseract_language_code


class OcrEngines:
    version = "1"

    def __init__(self, *, pool=None, cancelled=None):
        self.pool = pool
        self.cancelled = cancelled

    def _model_call_for(
        self,
        engine: str,
        path: Path,
        pages: list[dict],
        usage: dict,
        *,
        ctx: OpContext,
    ) -> ModelCallMeta:
        """The per-row OCR fact. ``units["pages"]`` is the METERED quantity
        the price book settles the per-page SKU against (``_METERED_UNIT_KEY``),
        so the receipt's "what did it cost" is the pages actually sent, not
        the pages estimated."""
        units: dict[str, Any] = {"pages": usage.get("input_pages", len(pages))}
        try:
            units["input_bytes"] = path.stat().st_size
        except OSError:
            pass
        if engine in LOCAL_ENGINES:
            return ModelCallMeta.local(
                capability=CAPABILITY_OCR,
                engine=engine,
                model_ids=[engine],
                units=units,
            )
        if engine in SIDECAR_ENGINES:
            units["requests"] = 1
            return ModelCallMeta.sidecar(
                capability=CAPABILITY_OCR,
                engine=engine,
                model_ids=[engine],
                units=units,
            )
        cost = usage.get("cost")
        units["requests"] = usage.get(
            "calls" if engine == DATALAB_ENGINE else "requests", 0
        )
        if engine == DATALAB_ENGINE:
            return ModelCallMeta.provider_call(
                capability=CAPABILITY_OCR,
                engine=engine,
                provider="datalab",
                provider_kind="platform_api",
                model_ids=[engine],
                credential_source=str(usage.get("credential_source") or "none"),
                provider_reported_cost_usd=cost,
                provider_cost_usd=cost,
                units=units,
                cost_source=(
                    "estimated"
                    if usage.get("cost_estimated")
                    else ("pricing_data" if cost is not None else "unknown")
                ),
                duration_ms=None,
            )
        provider = provider_from_model_id(engine)
        _, _, model = engine.partition("/")
        # The OBSERVED credential provenance (§0 truth invariant): what the
        # router actually selected for this provider, never a hardcoded
        # "local" — the divergence check on the binding epoch can only notice
        # a mismatch if the fact reports what happened.
        reader = getattr(
            (ctx.extras or {}).get("router"), "credential_source_for", None
        )
        return ModelCallMeta.provider_call(
            capability=CAPABILITY_OCR,
            engine=engine,
            provider=provider,
            provider_kind="platform_api",
            model_ids=[model or engine],
            credential_source=reader(provider) if callable(reader) else "none",
            provider_reported_cost_usd=cost,
            provider_cost_usd=cost,
            units={
                **units,
                "tokens_in": usage.get("in", 0),
                "tokens_out": usage.get("out", 0),
            },
            cost_source="pricing_data" if cost is not None else "unknown",
            warnings=[] if cost is not None else ["remote provider cost is unknown"],
            # Sum of what the router actually measured per page
            # (_ocr_vlm accumulates it on usage); None only when no page's
            # call was live-measured (all cached, or the transport isn't
            # bracketed — the local/sidecar/datalab branches above never
            # reach here).
            duration_ms=usage.get("duration_ms"),
        )

    async def run_engine_on_pages(
        self,
        engine: str,
        page_paths: list[Path],
        ctx: OpContext,
        *,
        usage: dict,
        language: str | None = None,
        scratch: Path,
    ) -> list[dict]:
        """THE engine dispatch, public: rendered page images -> per-page
        ``{text, blocks}`` dicts. Both the durable ``execute`` path and the
        compare preview call through here, so an engine in the family table is
        dispatchable everywhere. ``engine`` must already be canonical; billable
        engines accumulate into ``usage``."""
        if engine == LIGHT_ENGINE:
            return await self._ocr_rapidocr(page_paths, scratch, language)
        if engine == "tesseract":
            return await self._ocr_tesseract(page_paths, scratch, language)
        if engine in SIDECAR_ENGINES:
            return await self._ocr_sidecar(engine, page_paths, ctx)
        if engine == DATALAB_ENGINE:
            return await self._ocr_datalab(page_paths, ctx, usage)
        if "/" in engine:
            return await self._ocr_vlm(engine, page_paths, ctx, usage, language)
        local_names = ", ".join(sorted(LOCAL_ENGINES))
        sidecar_names = ", ".join(sorted(SIDECAR_ENGINES))
        raise ValueError(
            f"unknown ocr engine '{engine}' (local: {local_names}; "
            f"sidecar: {sidecar_names}; '{DATALAB_ENGINE}'; or a "
            "provider/model id)"
        )

    async def _page_images(
        self, path: Path, media: Any, spec: dict, scratch: Path
    ) -> list[Path]:
        """An image is one page; a PDF rasterizes to one PNG per page
        (poppler ``pdftoppm`` — rasterize-then-OCR, the decided route)."""
        if not path.exists():
            raise ValueError(f"ocr input not found: {path}")
        if not self._is_pdf(path, media):
            return [path]
        pdftoppm = shutil.which("pdftoppm")
        if not pdftoppm:
            logger.info(
                "OCR PDF rasterization unavailable",
                extra={
                    "event": "ocr_pdf_rasterized",
                    "status": "unavailable",
                    "requested_dpi": int(spec.get("dpi", DEFAULT_DPI)),
                    "duration_ms": 0.0,
                    "page_count": 0,
                    "rendered_bytes": 0,
                },
            )
            raise RuntimeError(
                "PDF OCR needs poppler (pdftoppm) on PATH — "
                "brew/apt install poppler(-utils)"
            )
        dpi = int(spec.get("dpi", DEFAULT_DPI))
        selected_pages = spec.get("_selected_pages")
        if selected_pages is not None and (
            not isinstance(selected_pages, list)
            or not selected_pages
            or any(type(page) is not int or page <= 0 for page in selected_pages)
            or len(selected_pages) != len(set(selected_pages))
        ):
            raise ValueError("selected PDF pages must be distinct positive integers")
        started = time.perf_counter()
        pages: list[Path] = []
        status = "failed"
        try:
            commands = []
            if selected_pages is None:
                commands.append(
                    [
                        pdftoppm,
                        "-png",
                        "-r",
                        str(dpi),
                        str(path),
                        str(scratch / "page"),
                    ]
                )
            else:
                commands.extend(
                    [
                        pdftoppm,
                        "-png",
                        "-r",
                        str(dpi),
                        "-f",
                        str(page),
                        "-l",
                        str(page),
                        "-singlefile",
                        str(path),
                        str(scratch / f"page-{page}"),
                    ]
                    for page in selected_pages
                )
            render_seconds = max(1, 600 // len(commands))
            for command in commands:
                result = await run_sandboxed(
                    command,
                    policy=SandboxPolicy(
                        cpu_seconds=render_seconds,
                        wall_seconds=render_seconds,
                        memory_mb=2048,
                        env_passthrough=["PATH"],
                        # Poppler parsing a PDF nobody vetted is the sharpest
                        # edge in this pipeline. It reads that one file and
                        # writes page rasters into the op's scratch directory.
                        #
                        # /etc/fonts is measured and load-bearing: without it a
                        # PDF whose fonts are NOT embedded still rasterizes and
                        # still exits 0, but fontconfig falls back and the pixels
                        # differ from the unconfined render -- which OCR would
                        # then read wrong. Pinned in
                        # tests/engine/test_sandbox_media_fence.py. The font files
                        # themselves are under /usr/share, already readable.
                        confine=fence.Confinement(
                            op="ocr (pdftoppm rasterization)",
                            read=(str(path), "/etc/fonts"),
                            write=(str(scratch),),
                            exec_binary=pdftoppm,
                        ),
                    ),
                    should_cancel=self.cancelled,
                )
                if result.cancelled:
                    raise OcrCancelled("OCR cancelled")
                if not result.ok:
                    raise RuntimeError(
                        f"pdf rasterization failed: {result.stderr[:300]}"
                    )
            if selected_pages is None:
                pages = sorted(
                    scratch.glob("page-*.png"),
                    key=lambda x: int(x.stem.rsplit("-", 1)[-1]),
                )
            else:
                pages = [scratch / f"page-{page}.png" for page in selected_pages]
                pages = [page for page in pages if page.is_file()]
            if not pages:
                raise RuntimeError("pdf rasterization produced no pages")
            if selected_pages is not None and len(pages) != len(selected_pages):
                raise RuntimeError(
                    "pdf rasterization did not produce every selected page"
                )
            status = "ok"
            return pages
        finally:
            observed_pages = pages or list(scratch.glob("page-*.png"))
            rendered_bytes = 0
            for page in observed_pages:
                try:
                    rendered_bytes += page.stat().st_size
                except OSError:
                    continue
            logger.info(
                "OCR PDF rasterization completed",
                extra={
                    "event": "ocr_pdf_rasterized",
                    "status": status,
                    "requested_dpi": dpi,
                    "duration_ms": round(
                        max(0.0, time.perf_counter() - started) * 1000, 3
                    ),
                    "page_count": len(observed_pages),
                    "rendered_bytes": rendered_bytes,
                },
            )

    @staticmethod
    def _is_pdf(path: Path, media: Any) -> bool:
        from frisket.ops.media_metadata import (
            _read_prefix,
            _recognize_prefix,
            _select_recognition,
        )

        del media
        prefix, _warnings = _read_prefix(path)
        recognition, _conflicts = _select_recognition(_recognize_prefix(prefix))
        return recognition is not None and recognition.get("kind") == "pdf"

    @staticmethod
    def _pdf_page_count(path: Path, digest: Any) -> Any:
        from frisket.ops.media_metadata import extract_media_metadata

        try:
            content_digest = digest if isinstance(digest, str) and digest else None
            if content_digest is None:
                sha256 = hashlib.sha256()
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        sha256.update(chunk)
                content_digest = sha256.hexdigest()
            envelope = extract_media_metadata(
                path,
                digest=content_digest,
                size_bytes=path.stat().st_size,
                filename=None,
                claimed_mime=None,
            )
        except Exception:  # noqa: BLE001 - an unavailable measure must fail closed
            return None
        normalized = envelope.get("normalized")
        return normalized.get("page_count") if isinstance(normalized, dict) else None

    async def _ocr_rapidocr(self, pages, scratch, language=None):
        return await local.ocr_rapidocr(self.pool, pages, scratch, language)

    @staticmethod
    async def _run_rapidocr_pool(pool, pages):
        return await local.run_rapidocr_pool(pool, pages)

    async def _ocr_tesseract(self, pages, scratch, language=None):
        return await local.ocr_tesseract(
            pages, scratch, language, cancelled=self.cancelled
        )

    async def _ocr_sidecar(self, engine, pages, ctx):
        return await sidecar.ocr_sidecar(engine, pages, ctx)

    async def _ocr_datalab(self, pages, ctx, usage):
        return await hosted.ocr_datalab(pages, ctx, usage)

    async def _ocr_vlm(self, model, pages, ctx, usage, language=None):
        return await hosted.ocr_vlm(
            model, pages, ctx, usage, language, recipe_version=self.version
        )
