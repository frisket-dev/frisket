"""Stateless model sidecar.

Blob routes accept multipart bytes, never application paths. The service fails
closed without ``FRISKET_MODELS_TOKEN`` and returns 429 rather than queueing
when its concurrency limit is full. Model routes require ``FRISKET_MODELS_TOKEN``; ``/health`` exposes no capability details.
"""

from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from frisket_models import __version__
from frisket_models.engines import (
    WHISPER_DESCRIPTOR,
    Registry,
    default_registry,
)
from frisket_models.transcription.config import worker_registry_from_env
from frisket_models.transcription.gateway import TranscriptionWorkerGateway
from frisket_models.transcription.gateway import WorkerGatewayError, WorkerRegistry
from frisket_models.transcription.gateway_api import (
    DEFAULT_TRANSCRIPTION_MAX_UPLOAD_BYTES,
    TranscriptionGateway,
    mount_gateway_transcribe,
)
from frisket_models.transcription.contract import (
    CONTRACT_VERSION,
    TranscribeOptions,
    TranscribeResult,
    TranscriptionError,
    TranscriptionErrorEnvelope,
)

DEFAULT_CONCURRENCY = 2
RETRY_AFTER_SECONDS = "2"


class Limiter:
    """Thread-safe, non-queueing concurrency limit."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._in_flight = 0
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        with self._lock:
            if self._in_flight >= self.limit:
                return False
            self._in_flight += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._in_flight -= 1

    @property
    def in_flight(self) -> int:
        return self._in_flight


@contextlib.contextmanager
def _slot(limiter: Limiter):
    if not limiter.acquire():
        raise HTTPException(
            status_code=429,
            detail="sidecar at capacity — retry shortly",
            headers={"Retry-After": RETRY_AFTER_SECONDS},
        )
    try:
        yield
    finally:
        limiter.release()


class EmbeddingsBody(BaseModel):
    """OpenAI-compatible embedding request; ``model`` is advisory."""

    input: str | list[str]
    model: str | None = None


class NerBody(BaseModel):
    texts: list[str]
    labels: list[str]
    threshold: float = 0.5
    engine: str = "gliner"


class RerankBody(BaseModel):
    query: str
    documents: list[str]
    top_k: int | None = None
    engine: str = "cross-encoder"


def _transcription_error(
    *, status_code: int, code: str, message: str, retryable: bool = False
) -> WorkerGatewayError:
    return WorkerGatewayError(
        status_code=status_code,
        envelope=TranscriptionErrorEnvelope(
            contract_version=CONTRACT_VERSION,
            error=TranscriptionError(
                code=code,
                message=message,
                retryable=retryable,
            ),
        ),
    )


class _SidecarTranscriptionGateway:
    """Serve the resident Faster Whisper engine and delegate other v1 engines."""

    def __init__(self, registry: Registry, remote: TranscriptionWorkerGateway) -> None:
        self._registry = registry
        self._remote = remote

    @property
    def registry(self) -> WorkerRegistry:
        """Expose the isolated-worker registry used for routing and diagnostics."""

        return self._remote.registry

    async def public_capabilities(self) -> list[dict[str, Any]]:
        return await self._remote.public_capabilities()

    async def transcribe(
        self,
        *,
        engine: str,
        audio_path: Path,
        options: TranscribeOptions,
    ) -> TranscribeResult:
        if engine != "whisper-turbo":
            return await self._remote.transcribe(
                engine=engine,
                audio_path=audio_path,
                options=options,
            )
        try:
            registered = self._registry.get(engine)
        except KeyError:
            raise _transcription_error(
                status_code=400,
                code="unsupported_engine",
                message="transcription engine is not supported",
            ) from None
        if registered.route != "/v1/transcribe":
            raise _transcription_error(
                status_code=400,
                code="unsupported_engine",
                message="transcription engine is not supported",
            )
        try:
            WHISPER_DESCRIPTOR.validate_options(options)
        except ValueError:
            raise _transcription_error(
                status_code=400,
                code="unsupported_option",
                message="transcription options are not supported by this engine",
            ) from None
        try:
            adapter = await run_in_threadpool(registered.get)
        except RuntimeError:
            raise _transcription_error(
                status_code=503,
                code="engine_unavailable",
                message="transcription engine is unavailable",
                retryable=True,
            ) from None
        try:
            return await run_in_threadpool(adapter, audio_path, options)
        except Exception:
            raise _transcription_error(
                status_code=500,
                code="worker_failure",
                message="transcription engine failed",
            ) from None


def create_app(
    token: str | None = None,
    registry: Registry | None = None,
    concurrency: int | None = None,
    transcription_gateway: TranscriptionGateway | None = None,
    transcription_max_upload_bytes: int | None = None,
    transcription_spool_dir: str | Path | None = None,
) -> FastAPI:
    token = token or os.environ.get("FRISKET_MODELS_TOKEN")
    if not token:
        raise RuntimeError(
            "FRISKET_MODELS_TOKEN is not set — the frisket-models sidecar is "
            "never anonymous. Generate a shared secret and "
            "set it on both the sidecar and the app."
        )
    if concurrency is None:
        concurrency = int(
            os.environ.get("FRISKET_MODELS_CONCURRENCY", DEFAULT_CONCURRENCY)
        )
    registry = registry or default_registry()
    if transcription_gateway is None:
        transcription_gateway = _SidecarTranscriptionGateway(
            registry,
            TranscriptionWorkerGateway(worker_registry_from_env()),
        )
    if transcription_max_upload_bytes is None:
        transcription_max_upload_bytes = int(
            os.environ.get(
                "FRISKET_TRANSCRIPTION_MAX_UPLOAD_BYTES",
                DEFAULT_TRANSCRIPTION_MAX_UPLOAD_BYTES,
            )
        )
    if transcription_max_upload_bytes < 1:
        raise ValueError("transcription_max_upload_bytes must be at least 1")
    resolved_transcription_spool_dir: Path | None = None
    if transcription_spool_dir is not None:
        resolved_transcription_spool_dir = Path(transcription_spool_dir).resolve()
        if not resolved_transcription_spool_dir.is_dir():
            raise ValueError("transcription_spool_dir must be an existing directory")
    limiter = Limiter(concurrency)

    app = FastAPI(title="frisket-models", version=__version__)
    app.state.registry = registry
    app.state.limiter = limiter
    app.state.transcription_gateway = transcription_gateway

    @app.get("/health")
    def health() -> dict:
        """Unauthenticated liveness with no capability details."""
        return {"ok": True}

    def _authed(request: Request) -> None:
        header = request.headers.get("Authorization")
        if not header:
            raise HTTPException(status_code=401, detail="missing bearer token")
        if header != f"Bearer {token}":
            raise HTTPException(status_code=403, detail="invalid bearer token")

    api = APIRouter(dependencies=[Depends(_authed)])

    async def _engine(route: str, name: str):
        """Resolve a route-compatible adapter; return 400 for caller errors
        and 503 for missing or failed engines."""
        try:
            engine = registry.get(name)
        except KeyError:
            options = ", ".join(e.name for e in registry.for_route(route)) or "none"
            raise HTTPException(
                status_code=400,
                detail=f"unknown engine '{name}' for {route} (loaded here: {options})",
            ) from None
        if engine.route != route:
            raise HTTPException(
                status_code=400,
                detail=f"engine '{name}' serves {engine.route}, not {route}",
            )
        return await _load(engine)

    async def _load(engine):
        """Load off the event loop; ``Engine.get`` serializes first use."""
        try:
            return await run_in_threadpool(engine.get)
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e)) from None

    @api.get("/capabilities")
    async def capabilities() -> dict:
        """Report unavailable extras and loader failures truthfully."""
        remote_transcription = await transcription_gateway.public_capabilities()
        resident = registry.describe()
        for entry in resident:
            if (
                entry["name"] == WHISPER_DESCRIPTOR.engine
                and entry["route"] == "/v1/transcribe"
            ):
                entry.update(
                    contract_versions=[CONTRACT_VERSION],
                    revision=WHISPER_DESCRIPTOR.revision,
                    runtime_image_id=WHISPER_DESCRIPTOR.runtime_image_id,
                    options=WHISPER_DESCRIPTOR.options.model_dump(mode="json"),
                )
        return {
            "service": "frisket-models",
            "version": __version__,
            "engines": [*resident, *remote_transcription],
            "concurrency": {"limit": limiter.limit, "in_flight": limiter.in_flight},
        }

    @api.post("/ocr")
    async def ocr(
        files: list[UploadFile],
        engine: str = Form("paddleocr-vl"),
    ) -> dict:
        """Return one text/block page per multipart part, in order."""
        adapter = await _engine("/ocr", engine)
        images = [await f.read() for f in files]
        with _slot(limiter):
            pages = await run_in_threadpool(adapter, images)
        return {"pages": pages}

    @api.post("/to-markdown")
    async def to_markdown(
        files: list[UploadFile],
        engine: str = Form("docling"),
    ) -> dict:
        """Return one markdown document per multipart part, in order."""
        adapter = await _engine("/to-markdown", engine)
        blobs = [(f.filename or "doc", await f.read()) for f in files]
        with _slot(limiter):
            documents = [
                await run_in_threadpool(adapter, name, data) for name, data in blobs
            ]
        return {"documents": documents}

    mount_gateway_transcribe(
        api,
        gateway=transcription_gateway,
        admission=limiter,
        max_upload_bytes=transcription_max_upload_bytes,
        spool_dir=resolved_transcription_spool_dir,
        retry_after_seconds=RETRY_AFTER_SECONDS,
    )

    @api.post("/ner")
    async def ner(body: NerBody) -> dict:
        """Return GLiNER entities aligned to input texts."""
        adapter = await _engine("/ner", body.engine)
        if not body.labels:
            raise HTTPException(status_code=400, detail="labels must be non-empty")
        with _slot(limiter):
            results = await run_in_threadpool(
                adapter, body.texts, body.labels, body.threshold
            )
        return {"results": results}

    @api.post("/rerank")
    async def rerank(body: RerankBody) -> dict:
        """Return document indices and scores in descending relevance."""
        adapter = await _engine("/rerank", body.engine)
        if not body.documents:
            return {"results": []}
        with _slot(limiter):
            scores = await run_in_threadpool(adapter, body.query, body.documents)
        ranked = sorted(
            ({"index": i, "score": s} for i, s in enumerate(scores)),
            key=lambda r: r["score"],
            reverse=True,
        )
        if body.top_k is not None:
            ranked = ranked[: body.top_k]
        return {"results": ranked}

    @api.post("/v1/embeddings")
    async def embeddings(body: EmbeddingsBody) -> dict:
        """OpenAI-compatible embeddings, preserving input order."""
        engine = registry.get("fastembed")
        adapter = await _load(engine)
        texts = [body.input] if isinstance(body.input, str) else body.input
        with _slot(limiter):
            vectors = await run_in_threadpool(adapter, texts)
        model_id = engine.models[0] if engine.models else "fastembed"
        return {
            "object": "list",
            "model": model_id,
            "data": [
                {"object": "embedding", "index": i, "embedding": v}
                for i, v in enumerate(vectors)
            ],
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }

    app.include_router(api)
    return app
