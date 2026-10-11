"""Route-bound transcription through a connected Frisket Cloud account."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from frisket.cloud_execution import (
    CloudMediaUpload,
    cloud_execution_accounting,
    cloud_media_execution,
)
from frisket.contracts.http.cloud_connection import CloudTranscriptionResult
from frisket.ops.base import OpContext, media_upload_filename
from frisket.ops.integrations.hosted_error import HostedEngineError

from .common import transcription_v1_options


class CloudTranscriptionAdapter:
    async def transcribe(
        self,
        engine: str,
        path: str,
        spec: dict[str, Any],
        ctx: OpContext,
        media: Any = None,
    ) -> dict[str, Any]:
        transport, attempt = cloud_media_execution(ctx, "transcribe", engine)
        source = Path(path)
        if not source.exists():
            raise ValueError(f"transcribe input not found: {path}")
        options = transcription_v1_options(engine, spec)
        file_name = media_upload_filename(path, media)
        content_type = (
            str(media["mime"])
            if isinstance(media, dict) and media.get("mime")
            else mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        )
        with source.open("rb") as stream:
            response = await transport.execute_media(
                attempt,
                capability="transcribe",
                engine=engine,
                options=options,
                uploads=(
                    CloudMediaUpload(
                        file_name=file_name,
                        content_type=content_type,
                        stream=stream,
                    ),
                ),
            )

        accounting = cloud_execution_accounting(
            attempt, response, capability="transcribe"
        )
        result = response.result
        if not isinstance(result, CloudTranscriptionResult):
            raise HostedEngineError(
                code="invalid_response",
                message="Cloud transcription returned no transcription result",
                accounting=accounting,
            )
        return {
            "text": result.text.strip(),
            "segments": [
                segment.model_dump(mode="json") for segment in result.segments
            ],
            "language": result.language,
            "duration": result.duration,
            "cost": accounting["cost"],
            "_cloud_model_calls": accounting["model_calls"],
            "cloud_receipt": accounting["cloud_receipt"],
        }

    def model_calls(
        self,
        engine: str,
        path: str,
        spec: dict[str, Any],
        out: dict[str, Any],
    ) -> list[dict[str, Any]]:
        del engine, path, spec
        calls = out.get("_cloud_model_calls")
        if not isinstance(calls, list) or not calls:
            raise RuntimeError("Cloud transcription result has no accounting facts")
        return [dict(call) for call in calls]
