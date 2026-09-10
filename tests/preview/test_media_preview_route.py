"""Free media previews consume one validated ephemeral route, never an attempt."""

import asyncio
import json
from dataclasses import replace

import pytest

from frisket.actions.media_options import OcrOptions, TranscriptionOptions
from frisket.actions.system import typed_action_for_request
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.executor.ocr_read import AdmittedOcrReader
from frisket.engine.executor.transcription_read import AdmittedTranscriber
from frisket.engine.runner import preview
from frisket.engine.runner.map_runner import MapRunner
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import media_cell, owned_media_metadata_document
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.execution.resolver import preview_resolution_in_scope
from frisket.ops.base import OpContext, RecipeInvocationHalt


@pytest.mark.parametrize("engine", ["dots.mocr", "whisper-turbo", "moss"])
def test_media_preview_uses_exact_validated_connection_without_writes(
    tmp_path, monkeypatch, engine
):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://validated.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "validated-token")
    ocr = engine == "dots.mocr"
    project = Project.create(tmp_path / "preview.frisket")
    try:
        sheet = project.add_sheet("Media")
        column = project.add_column(sheet, "source", type="image" if ocr else "audio")
        mime = "image/png" if ocr else "audio/wav"
        blob = project.add_blob(
            b"preview media fixture",
            filename="source.png" if ocr else "source.wav",
            mime=mime,
            metadata=owned_media_metadata_document(
                probe={"kind": "image", "width": 1, "height": 1}
                if ocr
                else {"kind": "audio", "duration_seconds": 1.0}
            ),
        )
        [row] = project.add_rows(
            sheet, [{"source": media_cell(blob, mime=mime)}], {"source": column}
        )
        plan = build_typed_map_rows_plan(
            project,
            typed_action_for_request(
                {
                    "action_id": "media.ocr" if ocr else "media.transcribe",
                    "scope": {
                        "kind": "sheet_rows",
                        "sheet_id": sheet,
                        "row_ids": [row],
                    },
                    "params": {"source": "source", "engine": engine},
                    "output_names": {"text": "read_text"},
                    "idempotency_key": "preview",
                }
            ),
        )
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            authority=UnroutedOnlyAuthority(project),
            op_context_extras={"preview_execution": object()},
        )
        validated = []
        original = preview._preview_validated

        def validate_then_change_ambient(*args, **kwargs):
            result = original(*args, **kwargs)
            validated.append(result.resolved_execution)
            # A fresh target lookup here would silently use another connection.
            monkeypatch.setenv("FRISKET_MODELS_URL", "http://changed.test")
            monkeypatch.setenv("FRISKET_MODELS_TOKEN", "changed-token")
            return result

        monkeypatch.setattr(preview, "_preview_validated", validate_then_change_ambient)
        calls = []

        async def post(ctx, path, **kwargs):
            connection = kwargs["connection"]
            assert connection is validated[0].resolution.connection
            assert connection.base_url == "http://validated.test"
            assert connection.token == "validated-token"
            calls.append(path)
            if ocr:
                return {"pages": [{"text": "hello", "blocks": []}]}
            return {
                "contract_version": "frisket.transcription.v1",
                "results": [
                    {
                        "engine": engine,
                        "text": "hello",
                        "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}],
                        "language": None if engine == "moss" else "en",
                        "duration": 1.0,
                        "model_ids": [engine],
                        "revision": "fixture",
                        "device": "cuda",
                        "dtype": "float16",
                        "timings": {"inference_seconds": 0.1},
                        "warnings": [],
                        "accepted_options": json.loads(kwargs["data"]["options"]),
                    }
                ],
            }

        if ocr:
            from frisket.ops import ocr_engines_sidecar

            monkeypatch.setattr(ocr_engines_sidecar, "sidecar_post", post)
        else:
            monkeypatch.setattr(
                "frisket.sdk.ops.transcription.sidecar.sidecar_post", post
            )
        before = tuple(project.db.iterdump())
        before_changes = project.db.total_changes
        spec = plan.spec_dict()
        result = asyncio.run(runner.preview(spec, program=plan.program))
        assert result.values[row]["read_text"]["value"] == "hello"
        assert calls == ["/ocr" if ocr else "/v1/transcribe"]
        assert len(validated) == 1
        assert tuple(project.db.iterdump()) == before
        assert project.db.total_changes == before_changes

        resolved = validated[0]
        assert resolved.persistence == "ephemeral"
        foreign = replace(
            resolved,
            resolution=replace(
                resolved.resolution,
                facts=replace(
                    resolved.resolution.facts, egress_class="third_party_api"
                ),
            ),
        )
        for extras in (
            {"preview": False, "preview_execution": resolved},
            {
                "preview": True,
                "preview_execution": replace(resolved, persistence="durable"),
            },
            {"preview": True, "preview_execution": foreign},
            {"preview": True, "preview_execution": {"resolution": resolved.resolution}},
        ):
            assert preview_resolution_in_scope(extras) is None
            ctx = OpContext(project=project, extras=extras)
            reader = (
                AdmittedOcrReader(
                    ctx, None, engine=engine, options=OcrOptions().normalize(engine)
                )
                if ocr
                else AdmittedTranscriber(
                    ctx, engine=engine, options=TranscriptionOptions().normalize(engine)
                )
            )
            with pytest.raises(RecipeInvocationHalt, match="requires|requires its"):
                asyncio.run(reader.start(expected_rows=1))
        assert len(calls) == 1
    finally:
        project.close()
