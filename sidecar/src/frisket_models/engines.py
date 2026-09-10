"""Lazy resident-engine registry with truthful capability errors."""

from __future__ import annotations

import importlib.util
import io
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from frisket_models.transcription.contract import (
    TranscribeOptions,
    TranscribeResult,
    TranscribeSegment,
    TranscribeWord,
    TranscriptionEngineDescriptor,
    TranscriptionOptionSupport,
)
from frisket_models.dots_mocr_identity import (
    ENGINE as DOTS_MOCR_ENGINE,
    MODEL_ID as DOTS_MOCR_MODEL_ID,
    MODEL_REVISION as DOTS_MOCR_MODEL_REVISION,
)
from frisket_models.glm_ocr_identity import (
    ENGINE as GLM_OCR_ENGINE,
    MODEL_ID as GLM_OCR_MODEL_ID,
    MODEL_REVISION as GLM_OCR_MODEL_REVISION,
)
from frisket_models.transcription.whisper_turbo import (
    WHISPER_TURBO_MODEL_ID as WHISPER_MODEL_ID,
    WHISPER_TURBO_MODEL_REVISION as WHISPER_MODEL_REVISION,
)

# Models download to the cache volume on first use; defaults favor CPU use.
GLINER_MODEL = os.environ.get("FRISKET_MODELS_GLINER", "urchade/gliner_multi-v2.1")
RERANK_MODEL = os.environ.get(
    "FRISKET_MODELS_RERANK", "cross-encoder/ms-marco-MiniLM-L-6-v2"
)
EMBED_MODEL = os.environ.get("FRISKET_MODELS_EMBED", "BAAI/bge-small-en-v1.5")
DOTS_MOCR_WORKER_URL_ENV = "FRISKET_OCR_DOTS_WORKER_URL"
DOTS_MOCR_WORKER_TOKEN_ENV = "FRISKET_OCR_DOTS_WORKER_TOKEN"
GLM_OCR_WORKER_URL_ENV = "FRISKET_OCR_GLM_WORKER_URL"
GLM_OCR_WORKER_TOKEN_ENV = "FRISKET_OCR_GLM_WORKER_TOKEN"
PADDLE_WORKER_URL_ENV = "FRISKET_OCR_PADDLE_WORKER_URL"
PADDLE_WORKER_TOKEN_ENV = "FRISKET_OCR_PADDLE_WORKER_TOKEN"
PP_OCRV6_ENGINE = "pp-ocrv6"
PP_OCRV6_WORKER_URL_ENV = "FRISKET_OCR_PP_OCRV6_WORKER_URL"
PP_OCRV6_WORKER_TOKEN_ENV = "FRISKET_OCR_PP_OCRV6_WORKER_TOKEN"


@dataclass
class Engine:
    """Import-probed engine with a lazy resident adapter."""

    name: str
    route: str
    modules: list[str]
    loader: Callable[[], Any]
    models: list[str] = field(default_factory=list)
    revision: str | None = None
    required_env: tuple[str, ...] = ()
    _adapter: Any = None
    _error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def installed(self) -> bool:
        if any(not os.environ.get(name) for name in self.required_env):
            return False
        try:
            return all(importlib.util.find_spec(m) is not None for m in self.modules)
        except (ImportError, ValueError):
            return False

    @property
    def loaded(self) -> bool:
        return self._adapter is not None

    @property
    def error(self) -> str | None:
        return self._error

    def get(self) -> Any:
        """Return the adapter or raise for a missing/failed engine."""
        if self._adapter is not None:
            return self._adapter
        if not self.installed:
            missing_env = [
                name for name in self.required_env if not os.environ.get(name)
            ]
            if missing_env:
                raise RuntimeError(
                    f"engine '{self.name}' is not configured — set "
                    + " and ".join(missing_env)
                )
            extra = EXTRAS.get(self.name, self.name)
            raise RuntimeError(
                f"engine '{self.name}' is not installed in this sidecar — "
                f"install the matching extra (uv pip install "
                f"'frisket-models[{extra}]')"
            )
        with self._lock:
            if self._adapter is None and self._error is None:
                try:
                    self._adapter = self.loader()
                except Exception as e:  # Retained so /capabilities reports the failure.
                    self._error = f"{type(e).__name__}: {e}"
            if self._error is not None:
                raise RuntimeError(
                    f"engine '{self.name}' failed to load: {self._error}"
                )
        return self._adapter

    def describe(self) -> dict:
        description = {
            "name": self.name,
            "route": self.route,
            "available": self.installed and self._error is None,
            "loaded": self.loaded,
            "models": self.models,
            "error": self._error,
        }
        if self.revision is not None:
            description["revision"] = self.revision
        return description


EXTRAS = {
    "surya2": "ocr",
    "paddleocr-vl": "ocr-paddle",
    PP_OCRV6_ENGINE: "ocr-paddle",
    "docling": "convert",
    "chandra": "convert-chandra",
    "gliner": "ner",
    "whisper-turbo": "transcribe",
    "cross-encoder": "rerank",
    "fastembed": "embed",
}


class Registry:
    """Engines keyed by client-visible name."""

    def __init__(self, engines: list[Engine]) -> None:
        self._engines = {e.name: e for e in engines}

    def get(self, name: str) -> Engine:
        engine = self._engines.get(name)
        if engine is None:
            raise KeyError(name)
        return engine

    def for_route(self, route: str) -> list[Engine]:
        return [e for e in self._engines.values() if e.route == route]

    def describe(self) -> list[dict]:
        return [e.describe() for e in self._engines.values()]


def _load_ocr_worker(
    *, engine: str, url_env: str, token_env: str
) -> Callable[[list[bytes]], list[dict]]:
    """Return a client for one isolated, authenticated OCR worker."""
    import httpx

    base_url = os.environ[url_env].rstrip("/")
    token = os.environ[token_env]

    def ocr_pages(images: list[bytes]) -> list[dict]:
        response = httpx.post(
            f"{base_url}/ocr",
            headers={"Authorization": f"Bearer {token}"},
            data={"engine": engine},
            files=[
                ("files", (f"page-{index}.png", image, "image/png"))
                for index, image in enumerate(images, start=1)
            ],
            follow_redirects=True,
            timeout=3600.0,
        )
        response.raise_for_status()
        payload = response.json()
        pages = payload.get("pages") if isinstance(payload, dict) else None
        if not isinstance(pages, list) or len(pages) != len(images):
            raise RuntimeError(f"{engine} worker returned an invalid page response")
        return pages

    return ocr_pages


def load_dots_mocr() -> Callable[[list[bytes]], list[dict]]:
    """Return a client for the isolated, authenticated dots.mocr worker."""
    return _load_ocr_worker(
        engine=DOTS_MOCR_ENGINE,
        url_env=DOTS_MOCR_WORKER_URL_ENV,
        token_env=DOTS_MOCR_WORKER_TOKEN_ENV,
    )


def load_glm_ocr() -> Callable[[list[bytes]], list[dict]]:
    """Return a client for the isolated, authenticated GLM-OCR worker."""
    return _load_ocr_worker(
        engine=GLM_OCR_ENGINE,
        url_env=GLM_OCR_WORKER_URL_ENV,
        token_env=GLM_OCR_WORKER_TOKEN_ENV,
    )


class _SuryaHtmlTextParser(HTMLParser):
    """Collect visible text from Surya's trusted model-output HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


def _surya_block_to_block(block: Any) -> dict[str, Any]:
    """Normalize one Surya 2 HTML block to Frisket's plain-text OCR shape."""
    parser = _SuryaHtmlTextParser()
    parser.feed(getattr(block, "html", "") or "")
    text = parser.text()
    normalized: dict[str, Any] = {"text": text}
    polygon = getattr(block, "polygon", None)
    if polygon:
        normalized["bbox"] = [[int(x), int(y)] for x, y in polygon]
    confidence = getattr(block, "confidence", None)
    if confidence is not None:
        normalized["score"] = round(float(confidence), 4)
    return normalized


def load_surya2() -> Callable[[list[bytes]], list[dict]]:
    """Surya 2 adapter for surya-ocr 0.22.x.

    The shared ``SuryaInferenceManager`` owns the upstream vLLM-or-llama.cpp
    backend. ``RecognitionPredictor`` performs full-page OCR and returns
    reading-ordered HTML blocks, which this adapter flattens to the sidecar's
    stable plain-text/block contract. Model assets download on first use.
    """
    from PIL import Image
    from surya.inference import SuryaInferenceManager
    from surya.recognition import RecognitionPredictor

    manager = SuryaInferenceManager()
    recognize = RecognitionPredictor(manager)

    def ocr_pages(images: list[bytes]) -> list[dict]:
        pils = [Image.open(io.BytesIO(raw)).convert("RGB") for raw in images]
        results = recognize(pils, full_page=True)
        pages = []
        for page in results:
            ordered = sorted(
                getattr(page, "blocks", None) or [],
                key=lambda block: getattr(block, "reading_order", 0),
            )
            blocks = [_surya_block_to_block(block) for block in ordered]
            blocks = [block for block in blocks if block["text"]]
            text = "\n".join(block["text"] for block in blocks).strip()
            pages.append({"text": text, "blocks": blocks})
        return pages

    return ocr_pages


def load_paddleocr_vl() -> Callable[[list[bytes]], list[dict]]:
    """PaddleOCR-VL 3.7 adapter.

    ``PaddleOCRVL.predict`` downloads its model and layout assets to
    ``~/.paddlex/official_models`` on first construction.
    """
    if os.environ.get(PADDLE_WORKER_URL_ENV) or os.environ.get(PADDLE_WORKER_TOKEN_ENV):
        return _load_ocr_worker(
            engine="paddleocr-vl",
            url_env=PADDLE_WORKER_URL_ENV,
            token_env=PADDLE_WORKER_TOKEN_ENV,
        )

    # Baidu BOS has served an expired certificate; operators may override this
    # default, and cached models require no network.
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "modelscope")

    from paddleocr import PaddleOCRVL

    options: dict[str, Any] = {"pipeline_version": "v1.6"}
    if server_url := os.environ.get("FRISKET_PADDLE_VL_REC_SERVER_URL"):
        options.update(
            {
                "vl_rec_backend": "vllm-server",
                "vl_rec_server_url": server_url,
                "vl_rec_api_model_name": "PaddleOCR-VL-1.6-0.9B",
            }
        )
    if device := os.environ.get("FRISKET_PADDLE_DEVICE"):
        if device.startswith("gpu"):
            import paddle

            if not paddle.is_compiled_with_cuda():
                raise RuntimeError(
                    "PaddleOCR-VL requires CUDA-enabled PaddlePaddle; install "
                    "paddlepaddle-gpu"
                )
        options["device"] = device
    predictor = PaddleOCRVL(**options)

    def ocr_pages(images: list[bytes]) -> list[dict]:
        import numpy as np
        from PIL import Image

        pages = []
        for raw in images:
            arr = np.array(Image.open(io.BytesIO(raw)).convert("RGB"))
            result = predictor.predict(arr)
            pages.append(_paddle_result_to_page(result))
        return pages

    return ocr_pages


def load_pp_ocrv6() -> Callable[[list[bytes]], list[dict]]:
    """PP-OCRv6 medium: fast conventional OCR with line geometry.

    PaddleOCR 3.7 makes the medium v6 detector/recognizer its stable default.
    Local/team sidecars run it directly; the lightweight hosted gateway proxies
    the same engine to its dependency-isolated Paddle worker.
    """
    if os.environ.get(PP_OCRV6_WORKER_URL_ENV) or os.environ.get(
        PP_OCRV6_WORKER_TOKEN_ENV
    ):
        return _load_ocr_worker(
            engine=PP_OCRV6_ENGINE,
            url_env=PP_OCRV6_WORKER_URL_ENV,
            token_env=PP_OCRV6_WORKER_TOKEN_ENV,
        )

    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "modelscope")

    from paddleocr import PaddleOCR

    options: dict[str, Any] = {
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        # PaddlePaddle 3.3.1's oneDNN executor cannot run the v6 detector's
        # PIR ArrayAttribute on current Python 3.13 builds. The ordinary
        # Paddle-static kernels work on CPU; CUDA workers ignore this CPU knob.
        "enable_mkldnn": False,
    }
    if device := os.environ.get("FRISKET_PADDLE_DEVICE"):
        if device.startswith("gpu"):
            import paddle

            if not paddle.is_compiled_with_cuda():
                raise RuntimeError(
                    "PP-OCRv6 requires CUDA-enabled PaddlePaddle; install "
                    "paddlepaddle-gpu"
                )
        options["device"] = device
    predictor = PaddleOCR(**options)

    def ocr_pages(images: list[bytes]) -> list[dict]:
        import numpy as np
        from PIL import Image

        pages = []
        for raw in images:
            arr = np.array(Image.open(io.BytesIO(raw)).convert("RGB"))
            pages.append(_pp_ocrv6_result_to_page(predictor.predict(arr)))
        return pages

    return ocr_pages


def _pp_ocrv6_result_to_page(result: Any) -> dict:
    """Normalize Paddle's reading-ordered line text, polygons, and scores."""
    item = result[0] if isinstance(result, list) and result else result
    texts = item.get("rec_texts")
    polygons = item.get("rec_polys")
    scores = item.get("rec_scores")
    texts = [] if texts is None else texts
    polygons = [] if polygons is None else polygons
    scores = [] if scores is None else scores

    blocks: list[dict[str, Any]] = []
    for index, raw_text in enumerate(texts):
        text = str(raw_text).strip()
        if not text:
            continue
        block: dict[str, Any] = {"text": text}
        if index < len(polygons) and polygons[index] is not None:
            polygon = polygons[index]
            if len(polygon) == 4:
                block["bbox"] = [[int(x), int(y)] for x, y in polygon]
        if index < len(scores) and scores[index] is not None:
            block["score"] = round(float(scores[index]), 4)
        blocks.append(block)
    return {
        "text": "\n".join(block["text"] for block in blocks),
        "blocks": blocks,
    }


def _paddle_result_to_page(result: Any) -> dict:
    """Normalize one PaddleOCR-VL result.

    Layout-ordered content is in ``parsing_res_list``. A ``spotting`` block's
    line-level ``rec_texts``/``rec_polys`` replace its coarse content to avoid
    double-counting. PaddleOCR-VL exposes no per-block confidence.
    """
    item = result[0] if isinstance(result, list) and result else result
    spotting = item.get("spotting_res") or {}
    spot_texts = spotting.get("rec_texts") or []
    spot_polys = spotting.get("rec_polys") or []

    blocks: list[dict[str, Any]] = []
    spotting_expanded = False
    for parsed in item.get("parsing_res_list") or []:
        if (
            getattr(parsed, "label", None) == "spotting"
            and spot_texts
            and not spotting_expanded
        ):
            for i, txt in enumerate(spot_texts):
                line: dict[str, Any] = {"text": str(txt)}
                if i < len(spot_polys) and spot_polys[i]:
                    line["bbox"] = [[int(x), int(y)] for x, y in spot_polys[i]]
                blocks.append(line)
            spotting_expanded = True
            continue
        content = getattr(parsed, "content", None)
        if not content:
            continue
        block: dict[str, Any] = {"text": str(content)}
        bbox = getattr(parsed, "bbox", None)
        if bbox is not None and len(bbox) == 4:
            x1, y1, x2, y2 = (int(v) for v in bbox)
            block["bbox"] = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
        blocks.append(block)
    text = " ".join(b["text"].strip() for b in blocks).strip()
    return {"text": text, "blocks": blocks}


def load_docling() -> Callable[[str, bytes], dict]:
    """Return markdown and per-page OCR provenance from Docling."""
    from docling.document_converter import DocumentConverter

    converter = DocumentConverter()

    def to_markdown(name: str, data: bytes) -> dict:
        # Docling requires a path and uses its suffix for format detection.
        suffix = Path(name).suffix or ".pdf"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
            scratch = f.name
        try:
            result = converter.convert(scratch)
            markdown = result.document.export_to_markdown()
            ocr_used = []
            for page in getattr(result, "pages", None) or []:
                cells = getattr(page, "cells", None) or []
                ocr_used.append(any(getattr(c, "from_ocr", False) for c in cells))
            return {"markdown": markdown, "ocr_used": ocr_used}
        finally:
            os.unlink(scratch)

    return to_markdown


def load_chandra() -> Callable[[str, bytes], dict]:
    """Chandra 2 HF adapter for chandra-ocr 0.2.x.

    ``load_file`` rasterizes each page; ``InferenceManager.generate`` returns
    per-page markdown. Every page is a full VLM decode, so ``ocr_used`` is
    always true. The HF backend is resident and impractically slow without a
    GPU; this sidecar does not manage Chandra's optional vLLM server.
    """
    from chandra.input import load_file
    from chandra.model import InferenceManager
    from chandra.model.schema import BatchInputItem

    model = InferenceManager(method="hf")

    def to_markdown(name: str, data: bytes) -> dict:
        suffix = Path(name).suffix or ".pdf"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
            scratch = f.name
        try:
            images = load_file(scratch, {})
            batch = [
                BatchInputItem(image=img, prompt_type="ocr_layout") for img in images
            ]
            results = model.generate(batch, include_images=False)
            markdown = "\n\n".join(r.markdown for r in results)
            return {"markdown": markdown, "ocr_used": [True] * len(results)}
        finally:
            os.unlink(scratch)

    return to_markdown


def load_gliner() -> Callable[[list[str], list[str], float], list[list[dict]]]:
    """Return GLiNER entities aligned to input texts."""
    from gliner import GLiNER

    model = GLiNER.from_pretrained(GLINER_MODEL)

    def extract(
        texts: list[str], labels: list[str], threshold: float
    ) -> list[list[dict]]:
        if hasattr(model, "batch_predict_entities"):
            raw = model.batch_predict_entities(texts, labels, threshold=threshold)
        else:
            raw = [
                model.predict_entities(t, labels, threshold=threshold) for t in texts
            ]
        return [
            [
                {
                    "text": e["text"],
                    "label": e["label"],
                    "start": e["start"],
                    "end": e["end"],
                    "score": round(float(e.get("score", 0.0)), 4),
                }
                for e in ents
            ]
            for ents in raw
        ]

    return extract


WHISPER_DESCRIPTOR = TranscriptionEngineDescriptor(
    engine="whisper-turbo",
    model_ids=[WHISPER_MODEL_ID],
    revision=WHISPER_MODEL_REVISION,
    runtime_image_id=None,
    options=TranscriptionOptionSupport(
        diarization_mode="none",
        speaker_hint="none",
        language=True,
        model_size=False,
        vad=True,
        context=True,
    ),
)


def load_faster_whisper() -> Callable[[Path, TranscribeOptions], TranscribeResult]:
    """Return the pinned Faster-Whisper adapter for transcription v1."""
    from faster_whisper import WhisperModel

    model = WhisperModel(
        WHISPER_MODEL_ID,
        revision=WHISPER_MODEL_REVISION,
        device="cpu",
        compute_type="int8",
    )

    def transcribe(path: Path, options: TranscribeOptions) -> TranscribeResult:
        WHISPER_DESCRIPTOR.validate_options(options)
        started = time.perf_counter()
        native_segments, info = model.transcribe(
            str(path),
            language=options.language,
            vad_filter=True if options.vad is None else options.vad,
            initial_prompt=options.context,
            word_timestamps=True,
        )
        segments = [
            TranscribeSegment(
                start=float(segment.start),
                end=float(segment.end),
                text=str(segment.text).strip(),
                words=(
                    [
                        TranscribeWord(
                            word=str(word.word).strip(),
                            start=float(word.start),
                            end=float(word.end),
                        )
                        for word in segment.words
                        if str(word.word).strip()
                    ]
                    if getattr(segment, "words", None) is not None
                    else None
                ),
            )
            for segment in native_segments
        ]
        detected_language = str(getattr(info, "language", "") or "").strip()
        reported_duration = getattr(info, "duration", None)
        duration = (
            float(reported_duration)
            if reported_duration is not None
            else max((segment.end for segment in segments), default=0.0)
        )
        result = TranscribeResult(
            engine=WHISPER_DESCRIPTOR.engine,
            text=" ".join(segment.text for segment in segments).strip(),
            segments=segments,
            language=detected_language or options.language,
            duration=duration,
            model_ids=list(WHISPER_DESCRIPTOR.model_ids),
            revision=WHISPER_DESCRIPTOR.revision,
            device="cpu",
            dtype="int8",
            timings={"adapter.inference_seconds": time.perf_counter() - started},
            warnings=[],
            accepted_options=options.supplied_options(),
        )
        return WHISPER_DESCRIPTOR.validate_result(result, options)

    return transcribe


def load_cross_encoder() -> Callable[[str, list[str]], list[float]]:
    """Return one relevance score per document, in input order."""
    from sentence_transformers import CrossEncoder

    model = CrossEncoder(RERANK_MODEL)

    def rerank(query: str, documents: list[str]) -> list[float]:
        scores = model.predict([(query, d) for d in documents])
        return [float(s) for s in scores]

    return rerank


def load_fastembed() -> Callable[[list[str]], list[list[float]]]:
    """Return vectors in input order."""
    from fastembed import TextEmbedding

    model = TextEmbedding(EMBED_MODEL)

    def embed(texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in model.embed(texts)]

    return embed


def default_registry() -> Registry:
    paddle_worker_configured = bool(
        os.environ.get(PADDLE_WORKER_URL_ENV) or os.environ.get(PADDLE_WORKER_TOKEN_ENV)
    )
    pp_ocrv6_worker_configured = bool(
        os.environ.get(PP_OCRV6_WORKER_URL_ENV)
        or os.environ.get(PP_OCRV6_WORKER_TOKEN_ENV)
    )
    return Registry(
        [
            Engine(
                DOTS_MOCR_ENGINE,
                "/ocr",
                ["httpx"],
                load_dots_mocr,
                models=[DOTS_MOCR_MODEL_ID],
                revision=DOTS_MOCR_MODEL_REVISION,
                required_env=(
                    DOTS_MOCR_WORKER_URL_ENV,
                    DOTS_MOCR_WORKER_TOKEN_ENV,
                ),
            ),
            Engine(
                GLM_OCR_ENGINE,
                "/ocr",
                ["httpx"],
                load_glm_ocr,
                models=[GLM_OCR_MODEL_ID],
                revision=GLM_OCR_MODEL_REVISION,
                required_env=(
                    GLM_OCR_WORKER_URL_ENV,
                    GLM_OCR_WORKER_TOKEN_ENV,
                ),
            ),
            Engine(
                "surya2",
                "/ocr",
                ["surya"],
                load_surya2,
                models=["datalab-to/surya-ocr-2"],
            ),
            Engine(
                "paddleocr-vl",
                "/ocr",
                ["httpx"] if paddle_worker_configured else ["paddleocr"],
                load_paddleocr_vl,
                models=["PaddleOCR-VL-1.6"],
                required_env=(
                    PADDLE_WORKER_URL_ENV,
                    PADDLE_WORKER_TOKEN_ENV,
                )
                if paddle_worker_configured
                else (),
            ),
            Engine(
                PP_OCRV6_ENGINE,
                "/ocr",
                ["httpx"] if pp_ocrv6_worker_configured else ["paddleocr"],
                load_pp_ocrv6,
                models=["PP-OCRv6_medium_det", "PP-OCRv6_medium_rec"],
                required_env=(
                    PP_OCRV6_WORKER_URL_ENV,
                    PP_OCRV6_WORKER_TOKEN_ENV,
                )
                if pp_ocrv6_worker_configured
                else (),
            ),
            Engine(
                "docling",
                "/to-markdown",
                ["docling"],
                load_docling,
                models=["docling"],
            ),
            Engine(
                "chandra",
                "/to-markdown",
                ["chandra"],
                load_chandra,
                models=["chandra-ocr-2"],
            ),
            Engine("gliner", "/ner", ["gliner"], load_gliner, models=[GLINER_MODEL]),
            Engine(
                "whisper-turbo",
                "/v1/transcribe",
                ["faster_whisper"],
                load_faster_whisper,
                models=[WHISPER_MODEL_ID],
            ),
            Engine(
                "cross-encoder",
                "/rerank",
                ["sentence_transformers"],
                load_cross_encoder,
                models=[RERANK_MODEL],
            ),
            Engine(
                "fastembed",
                "/v1/embeddings",
                ["fastembed"],
                load_fastembed,
                models=[EMBED_MODEL],
            ),
        ]
    )
