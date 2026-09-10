"""Read-only translation comparison preview (scratch bake-off).

The text-source sibling of ``ocr`` / ``transcribe``: it runs a
TEXT sample through several translate engines side by side and returns each
engine's translation + detected source language — WITHOUT writing runs,
results, receipts, ops, rows, columns, evidence, or blobs. Nothing is persisted.

Engine dispatch reuses the typed action host's ``TranslationEngine`` — the same
per-engine paths the durable ``map.translate`` action runs — so a stubbed
engine (an injected ``httpx.MockTransport`` client) drives this exactly as it
drives the real action, and tests never touch the live network by default.

FREE ENGINES ONLY. Billable → run; not billable → preview — one predicate,
no preview taxonomy. ``llm``, ``deepl`` and ``google_translate`` all bill real
money, and running them here produced no receipt, no attempt, no egress record
and no spend line, so they are refused and pointed at the cost-gated
``map.translate`` action. The local ``opus_mt``/``hy_mt2`` engines run on the
operator's own box and keep working exactly as before. The refusal is
structural, not a flag: there is no ``allow_remote`` and no ``router``
parameter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from frisket.preview.common import (
    BillablePreviewDispatch,
    ComparePreviewError,
    billable_engine_error,
)
from frisket.ops.integrations.translate_common import HOSTED_TRANSLATE_ENGINES
from frisket.ops.base import OpContext
from frisket.ops.integrations.translation_engine import TranslationEngine
from frisket.redaction import canonical_error_code, redact_text, safe_error
from frisket.engine.store import Project

SCHEMA_VERSION = "frisket.translate_compare_preview.v1"

# Cap the compared engine set + sample size so a bake-off stays a quick,
# bounded, low-cost probe (the whole point is a cheap side-by-side).
MAX_COMPARE_ENGINES = 6
MAX_SAMPLE_CHARS = 20_000

# Engines that reach off-box / bill money. The scratch preview REFUSES these
# outright and names map.translate instead (mirrors ocr/transcribe compare).
_REMOTE_ENGINES = frozenset({"llm", *HOSTED_TRANSLATE_ENGINES})


class TranslateComparePreviewError(ComparePreviewError):
    """Validation/runtime error returned as a preview 400."""


@dataclass(frozen=True)
class TranslateCompareScratchRequest:
    """Bake-off request over a pasted TEXT sample — no project row/sheet/blob.

    ``language`` is the shared always-a-list source hint ([] =
    auto). There is no ``model`` field: it only ever fed the ``llm`` engine,
    which bills and is now refused here.
    """

    engines: list[str]
    text: str
    target_language: str = "English"
    language: list[str] | None = None


def _coerce_scratch_request(
    request: TranslateCompareScratchRequest | Mapping[str, Any],
) -> TranslateCompareScratchRequest:
    if isinstance(request, TranslateCompareScratchRequest):
        req = request
    elif isinstance(request, Mapping):
        raw_engines = request.get("engines")
        engines = (
            [str(e).strip() for e in raw_engines if str(e).strip()]
            if isinstance(raw_engines, list)
            else []
        )
        language = request.get("language")
        if isinstance(language, str):
            language = [language]
        req = TranslateCompareScratchRequest(
            engines=engines,
            text=str(request.get("text") or ""),
            target_language=str(request.get("target_language") or "English"),
            language=[str(x) for x in language] if isinstance(language, list) else None,
        )
    else:
        raise TranslateComparePreviewError(
            "invalid_request", "translate compare request must be an object"
        )

    if not req.engines:
        raise TranslateComparePreviewError(
            "invalid_request", "select at least one engine", field="engines"
        )
    if len(req.engines) > MAX_COMPARE_ENGINES:
        raise TranslateComparePreviewError(
            "invalid_request",
            f"compare at most {MAX_COMPARE_ENGINES} engines at once",
            field="engines",
        )
    if not req.text.strip():
        raise TranslateComparePreviewError(
            "invalid_request", "paste some text to translate", field="text"
        )
    if len(req.text) > MAX_SAMPLE_CHARS:
        raise TranslateComparePreviewError(
            "invalid_request",
            f"sample is too large (max {MAX_SAMPLE_CHARS} characters)",
            field="text",
        )
    if not req.target_language.strip():
        raise TranslateComparePreviewError(
            "invalid_request", "choose a target language", field="target_language"
        )
    # Billable → run. Refuse the WHOLE request rather than degrading the
    # billable engines to per-engine errors inside a 200, matching ocr and
    # transcribe: one predicate, one refusal shape, on every compare surface.
    for engine in req.engines:
        if engine in _REMOTE_ENGINES:
            raise billable_engine_error(
                engine,
                error=TranslateComparePreviewError,
                action_kind="map.translate",
            )
    return req


def _spec_for(engine: str, req: TranslateCompareScratchRequest) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "engine": engine,
        "target_language": req.target_language,
        "output_name": "translation",
        # The compare always wants the detected language shown per engine.
        "save_detected_language": True,
    }
    if req.language:
        spec["language"] = req.language
    return spec


async def compare_translate_scratch(
    project: Project | None,
    request: TranslateCompareScratchRequest | Mapping[str, Any],
    *,
    http: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Bake-off translation through the shared local engine host, without writes."""
    req = _coerce_scratch_request(request)
    owns_http = http is None
    client = http or httpx.AsyncClient()
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    try:
        for engine in req.engines:
            started = time.perf_counter()
            try:
                out = await _run_engine_with_project(
                    engine, req, http=client, project=project
                )
                runtime_ms = int((time.perf_counter() - started) * 1000)
                results.append(
                    {
                        "engine": engine,
                        "translation": out["translation"],
                        "detected_language": out["detected_language"],
                        "runtime_ms": runtime_ms,
                        "errors": [],
                    }
                )
            except BillablePreviewDispatch:
                # Never degraded into a per-engine error: a billable engine
                # reaching dispatch means the refusal in _coerce_* has a hole.
                raise
            except Exception as exc:  # noqa: BLE001 - preview reports engine failure
                runtime_ms = int((time.perf_counter() - started) * 1000)
                try:
                    candidate_code = getattr(exc, "code", None)
                except BaseException:  # diagnostics must not mask the engine failure
                    candidate_code = None
                fallback_code = "translate_preview_engine_failed"
                code = fallback_code
                if type(candidate_code) is str:
                    canonical_code = canonical_error_code(
                        candidate_code,
                        fallback=fallback_code,
                    )
                    if (
                        canonical_code == candidate_code
                        and redact_text(candidate_code, max_chars=64) == candidate_code
                    ):
                        code = canonical_code
                safe = safe_error(code, exc, max_chars=300)
                errors.append(
                    {"code": safe.code, "engine": engine, "message": safe.detail}
                )
                results.append(
                    {
                        "engine": engine,
                        "translation": "",
                        "detected_language": None,
                        "runtime_ms": runtime_ms,
                        "errors": [safe.detail],
                    }
                )
    finally:
        if owns_http:
            await client.aclose()

    return {
        "schema_version": SCHEMA_VERSION,
        "source": {"scratch": True, "text_length": len(req.text)},
        "target_language": req.target_language,
        "engines": list(req.engines),
        "results": results,
        "warnings": [],
        "errors": errors,
    }


async def _run_engine_with_project(
    engine: str,
    req: TranslateCompareScratchRequest,
    *,
    http: httpx.AsyncClient,
    project: Project | None,
) -> dict[str, Any]:
    spec = _spec_for(engine, req)
    row_values = {"text": req.text}
    if engine in _REMOTE_ENGINES:
        # The effect-site fence. ``_coerce_scratch_request`` already refused
        # this with a 400; reaching here means that refusal has a hole, which
        # is a bug to surface loudly, never a per-engine 200. There is no
        # router in scope, so ``llm`` has no completer and the hosted engines
        # would otherwise reach for project secrets and spend.
        raise BillablePreviewDispatch(
            f"translate compare tried to dispatch billable engine '{engine}'; "
            "billable engines belong to the cost-gated map.translate action"
        )
    ctx = OpContext(project=project, http=http, extras={})
    out = await TranslationEngine().execute(row_values, spec, ctx)
    data = out[0] if isinstance(out, tuple) else out
    return {
        "translation": str(data.get("translation") or ""),
        "detected_language": data.get("detected_language"),
    }
