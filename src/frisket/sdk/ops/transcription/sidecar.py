"""Versioned sidecar transcription adapter and gateway connection helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from frisket.ai.models.metadata import ModelCallMeta
from frisket.contracts.transcription_sidecar import (
    TRANSCRIPTION_CONTRACT_VERSION,
    TRANSCRIPTION_ENDPOINT,
)
from frisket.execution.attempt import routed_admission_in_scope
from frisket.execution.resolver import preview_resolution_in_scope
from frisket.ops._sidecar import ephemeral_gateway_connection, sidecar_post
from frisket.ops.base import OpContext

from .common import (
    input_units,
    transcription_v1_options,
    transcription_v1_result,
    v1_provenance_units,
)

DEFAULT_SIDECAR_TRANSCRIBE_TIMEOUT_SECONDS = 3600.0
SIDECAR_TRANSCRIBE_CONNECT_TIMEOUT_SECONDS = 10.0
SIDECAR_TRANSCRIBE_WRITE_TIMEOUT_SECONDS = 300.0
SIDECAR_TRANSCRIBE_POOL_TIMEOUT_SECONDS = 10.0


def sidecar_transcribe_timeout(connection: Any) -> httpx.Timeout:
    """Use the admitted connection; dispatch must not reread placement env vars."""

    if connection is not None and connection.timeout_seconds:
        return httpx.Timeout(
            connect=connection.connect_timeout_seconds
            or SIDECAR_TRANSCRIBE_CONNECT_TIMEOUT_SECONDS,
            read=float(connection.timeout_seconds),
            write=SIDECAR_TRANSCRIBE_WRITE_TIMEOUT_SECONDS,
            pool=SIDECAR_TRANSCRIBE_POOL_TIMEOUT_SECONDS,
        )
    return httpx.Timeout(
        connect=SIDECAR_TRANSCRIBE_CONNECT_TIMEOUT_SECONDS,
        read=DEFAULT_SIDECAR_TRANSCRIBE_TIMEOUT_SECONDS,
        write=SIDECAR_TRANSCRIBE_WRITE_TIMEOUT_SECONDS,
        pool=SIDECAR_TRANSCRIBE_POOL_TIMEOUT_SECONDS,
    )


def route_gateway_connection(ctx: OpContext, engine: str, *, light_engine: str) -> Any:
    admission = routed_admission_in_scope(ctx.extras)
    if admission is not None:
        return admission.binding.connection
    preview = preview_resolution_in_scope(ctx.extras)
    if preview is not None:
        return preview.connection
    return ephemeral_gateway_connection(
        op="transcribe", engine=engine, light_engine=light_engine
    )


class TranscriptionV1Adapter:
    async def transcribe(
        self,
        engine: str,
        path: str,
        spec: dict,
        ctx: OpContext,
        *,
        light_engine: str = "faster_whisper",
    ) -> dict[str, Any]:
        p = Path(path)
        if not p.exists():
            raise ValueError(f"transcribe input not found: {path}")
        options = transcription_v1_options(engine, spec)
        options_json = json.dumps(
            options.model_dump(exclude_none=True, mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        connection = route_gateway_connection(ctx, engine, light_engine=light_engine)
        body = await sidecar_post(
            ctx,
            TRANSCRIPTION_ENDPOINT,
            files=[
                (
                    "files",
                    (p.name or "audio", p.read_bytes(), "application/octet-stream"),
                )
            ],
            data={
                "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
                "engine": engine,
                "options": options_json,
            },
            op="transcribe",
            light_engine=light_engine,
            structured_errors=True,
            timeout=sidecar_transcribe_timeout(connection),
            connection=connection,
        )
        return transcription_v1_result(
            body,
            wire_engine=engine,
            requested_options=options,
            boundary="sidecar",
        )

    def model_calls(
        self, engine: str, path: str, spec: dict[str, Any], out: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return [
            ModelCallMeta.sidecar(
                capability="transcribe",
                engine=engine,
                model_ids=list(out.get("model_ids") or [engine]),
                units=v1_provenance_units(
                    out, {**input_units(path, out), "requests": 1}
                ),
                warnings=[str(item) for item in (out.get("warnings") or [])],
            ).as_dict()
        ]
