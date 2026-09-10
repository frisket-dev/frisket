"""An ``off`` egress policy must not block local or sidecar work.

``local`` runs in-process; ``sidecar`` is the operator's own configured
``frisket-models`` service. Only hosted engines and remote LLM providers are
network-gated."""

from __future__ import annotations

import pytest

from frisket.engine.runner import validation
from frisket.engine.runner.map_runner import MapRunner
from frisket.engine.runner.network_policy import remote_capability_for_spec
from frisket.ai.llm import ModelRouter
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from frisket.engine.store import Project
from frisket.execution.attempt_authority import UnroutedOnlyAuthority


def _local_endpoint() -> LocalModelEndpointConfig:
    return LocalModelEndpointConfig(
        endpoint_id="test-local",
        display_name="Test local",
        origin="http://127.0.0.1:11434",
        source="local_file",
    )


def _engine_program(
    recipe_name, engine, *, project=None, sheet_id=1, source="document", **options
):
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import (
        _typed_map_rows_plan,
        build_typed_map_rows_plan,
    )

    if recipe_name in {"map.translate", "map.classify"}:
        source = [source]
    if engine == "opus_mt":
        options.setdefault("language", ["en"])
        options.setdefault("target_language", "Spanish")
    if recipe_name == "map.classify":
        options.setdefault(
            "fields",
            [{"name": "relevance", "type": "score", "description": "relevance"}],
        )
    bound = typed_action_for_request(
        {
            "action_id": recipe_name,
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"source": source, "engine": engine, **options},
            "idempotency_key": "network-policy",
        }
    )
    plan = (
        build_typed_map_rows_plan(project, bound)
        if project is not None
        else _typed_map_rows_plan(bound)
    )
    return plan.program, plan.spec_dict()


@pytest.mark.parametrize(
    ("recipe_name", "engine"),
    [
        ("map.translate", "opus_mt"),
        ("map.translate", "hy_mt2"),
        ("media.ocr", "rapidocr"),
        ("media.ocr", "tesseract"),
        ("media.ocr", "dots.mocr"),  # sidecar
        ("media.ocr", "paddleocr-vl"),  # sidecar
        ("media.transcribe", "faster_whisper"),
        ("media.transcribe", "parakeet-tdt"),
        ("media.transcribe", "whisper-turbo"),  # sidecar
        ("media.to_markdown", "markitdown"),
        ("media.to_markdown", "docling"),  # sidecar
        ("media.to_markdown", "chandra"),  # sidecar
    ],
)
def test_local_and_sidecar_engines_are_not_remote(recipe_name, engine) -> None:
    recipe, spec = _engine_program(recipe_name, engine)
    assert remote_capability_for_spec(recipe, spec) is None, (
        f"{recipe_name}/{engine} must not be network-gated"
    )


@pytest.mark.parametrize(
    ("recipe_name", "engine", "expected"),
    [
        ("map.translate", "deepl", "engine:deepl"),
        ("map.translate", "google_translate", "engine:google_translate"),
        ("media.ocr", "datalab", "engine:datalab"),
        ("media.ocr", "openai/gpt-5-mini", "provider:openai"),  # slashed remote VLM
        # Provider-qualified transcription ids classify by their provider.
        ("media.transcribe", "openai/whisper-1", "provider:openai"),
        ("media.to_markdown", "datalab", "engine:datalab"),
    ],
)
def test_hosted_engines_are_remote(recipe_name, engine, expected) -> None:
    recipe, spec = _engine_program(recipe_name, engine)
    assert remote_capability_for_spec(recipe, spec) == expected


def test_parakeet_gateway_is_trusted_sidecar() -> None:
    # Diarization changes worker capability, not the operator-configured
    # gateway's trusted-sidecar network-policy class.
    for diarize in (False, True):
        recipe, spec = _engine_program(
            "media.transcribe", "parakeet-tdt", diarize=diarize
        )
        assert (
            remote_capability_for_spec(
                recipe,
                spec,
            )
            is None
        )


def test_ollama_llm_rows_are_not_remote() -> None:
    recipe, spec = _engine_program(
        "map.classify", "llm", model="ollama/@test-local/qwen3:8b"
    )
    assert remote_capability_for_spec(recipe, spec) is None


def test_remote_llm_provider_is_remote_and_unknown_fails_closed() -> None:
    for model, expected in (
        ("openai/gpt-5", "provider:openai"),
        ("mysterycorp/model-x", "provider:mysterycorp"),
    ):
        recipe, spec = _engine_program("map.classify", "llm", model=model)
        assert remote_capability_for_spec(recipe, spec) == expected


def test_always_remote_kinds_classify_from_the_catalog() -> None:
    from frisket.ops.base import Recipe

    recipe = Recipe()
    for kind, tag in (
        ("enrich.geocode", "external:geocode"),
        ("enrich.census_demographics", "external:us_census_acs"),
        ("media.fetch_url", "external:media_download"),
        ("web.capture_page", "external:url_capture"),
        ("research.web_search", "external:web_search"),
    ):
        assert remote_capability_for_spec(recipe, {"action_kind": kind}) == tag, kind


def test_off_project_still_validates_local_translate_run(tmp_path) -> None:
    """End-to-end: _validate_spec under ``off`` lets a local engine through
    (any failure must not be NetworkDisabled)."""
    project = Project.create(tmp_path / "p.frisket")
    try:
        sheet = project.add_sheet("data")
        cols = {"statement": project.add_column(sheet, "statement")}
        project.add_rows(sheet, [{"statement": "Hello"}], cols)
        project.set_network_policy(mode="off")
        runner = MapRunner(
            project,
            ModelRouter(cache=None, cache_mode="off"),
            authority=UnroutedOnlyAuthority(project),
        )
        program, spec = _engine_program(
            "map.translate",
            "opus_mt",
            project=project,
            sheet_id=sheet,
            source="statement",
        )
        validation.validate_spec(
            runner.project,
            runner.router,
            runner.run_store,
            spec,
            program=program,
            confirmed=True,
            resume_run_id=None,
            pricing_policy=runner.pricing_policy,
            composition=runner.execution_composition,
        )
    finally:
        project.close()


def test_off_project_any_canonical_ollama_run_never_cost_gates(tmp_path) -> None:
    """An unlisted local model is known zero and remains allowed offline."""
    project = Project.create(tmp_path / "p.frisket")
    try:
        sheet = project.add_sheet("data")
        cols = {"text": project.add_column(sheet, "text")}
        project.add_rows(sheet, [{"text": "Hello"}], cols)
        project.set_network_policy(mode="off")
        runner = MapRunner(
            project,
            ModelRouter(
                cache=None,
                cache_mode="off",
                local_endpoints=(_local_endpoint(),),
            ),
            authority=UnroutedOnlyAuthority(project),
        )
        program, spec = _engine_program(
            "map.classify",
            "llm",
            project=project,
            sheet_id=sheet,
            source="text",
            model="ollama/@test-local/frisket-new-unlisted-model",
        )
        validation.validate_spec(
            runner.project,
            runner.router,
            runner.run_store,
            spec,
            program=program,
            confirmed=False,
            resume_run_id=None,
            pricing_policy=runner.pricing_policy,
            composition=runner.execution_composition,
        )  # must not raise ClaimsGate or NetworkDisabled
    finally:
        project.close()
