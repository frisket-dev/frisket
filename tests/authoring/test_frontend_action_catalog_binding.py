from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.authoring.plugin_registry import default_registry
from frisket.server.app import create_app


ROOT = Path(__file__).resolve().parents[2]
ACTION_MODEL_SOURCE = "web/src/actions/model.ts"
ACTION_REGISTRY_SOURCE = "web/src/actions/registry.ts"
# The action-catalog fetch + merge-into-templates step moved out of
# ActionPanel.tsx into the workspace shell that mounts it, so the fetch-site
# assertions below follow it here.
WORKSPACE_MODEL_SOURCE = "web/src/workspace/useWorkspaceModel.tsx"
# RETARGET 2026-07-12 (actionpanel-decomposition-v1): ActionPanel.tsx is the
# launcher/drawer shell only; the ActionForm orchestrator (and every api.*
# call the form makes) lives under web/src/components/action-panel/. The
# no-legacy-recipe-fetch bans below cover both files.
ACTION_PANEL_SOURCE = "web/src/components/ActionPanel.tsx"
ACTION_FORM_SOURCE = "web/src/components/action-panel/ActionForm.tsx"
TRANSCRIBE_BUILTIN_DECLARATIONS = (
    ROOT / "src/frisket/data/transcribe_builtin_declarations.json"
)
FRONTEND_TRANSCRIBE_BUILTIN_DECLARATIONS = (
    ROOT / "web/src/assets/transcribe_builtin_declarations.json"
)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _catalog_entry(payload: dict, kind: str) -> dict:
    matches = [entry for entry in payload["actions"] if entry["kind"] == kind]
    assert len(matches) == 1
    return matches[0]


def test_frontend_transcribe_builtin_declarations_match_python_engine_table() -> None:
    """Keep catalog-failure options and stale-spec aliases on backend truth."""
    from frisket.contracts.actions.schemas._engines import TRANSCRIBE_ENGINE_TABLE

    expected_engines = {}
    for entry in TRANSCRIBE_ENGINE_TABLE:
        capabilities = entry.transcription
        assert capabilities is not None
        expected_engines[entry.id] = {
            "aliases": list(entry.aliases),
            "transcription_options": {
                "language": capabilities.accepts_language,
                "vad": capabilities.vad,
                "model_size": capabilities.model_size,
                "context": capabilities.context,
                "clean": capabilities.clean,
            },
        }

    # rule19: two-sources: frontend-consumed artifact diffed against the Python engine table
    actual = json.loads(TRANSCRIBE_BUILTIN_DECLARATIONS.read_text(encoding="utf-8"))
    assert actual == {
        "schema_version": "frisket.transcribe_builtin_declarations.v1",
        "engines": expected_engines,
    }
    assert json.loads(
        # rule19: two-sources: packaged frontend declarations vs canonical backend artifact
        FRONTEND_TRANSCRIBE_BUILTIN_DECLARATIONS.read_text(encoding="utf-8")
    ) == json.loads(
        # rule19: two-sources: canonical backend declarations vs packaged frontend artifact
        TRANSCRIBE_BUILTIN_DECLARATIONS.read_text(encoding="utf-8")
    )


def test_action_catalog_http_payload_carries_launcher_hints(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    client = TestClient(
        create_app(workspace, router=ModelRouter(cache=None, cache_mode="off"))
    )

    response = client.get("/api/actions/v1/catalog")
    assert response.status_code == 200
    payload = response.json()
    catalog_kinds = {entry["kind"] for entry in payload["actions"]}
    registered_kinds = {spec.action_kind for spec in default_registry().recipe_specs()}
    # The registry is keyed by the same canonical action identity the catalog
    # serves; private recipe implementation names are not part of this check.
    missing_catalog_kinds = registered_kinds - catalog_kinds
    assert not missing_catalog_kinds

    geocode = _catalog_entry(payload, "enrich.geocode")
    geocode_hints = geocode["ui_hints"]
    assert geocode_hints["uses_model"] is False
    assert geocode_hints["source_requirements"]
    assert "pricing" not in geocode_hints
    # Nominatim remains an external-egress option, but a free public API is
    # not represented as a zero-dollar tariff. OpenCage retains its genuine
    # own-provider estimate.
    assert set(geocode_hints["pricing_options"]) == {"opencage"}
    assert geocode_hints["cost_source"] == "free_public_api"
    assert geocode_hints["cost_source_options"] == {"nominatim": "free_public_api"}

    census_hints = _catalog_entry(payload, "enrich.census_demographics")["ui_hints"]
    assert "pricing" not in census_hints
    assert census_hints["cost_source"] == "free_public_api"

    transcribe = _catalog_entry(payload, "media.transcribe")
    transcribe_hints = transcribe["ui_hints"]
    assert transcribe_hints["uses_model"] is False
    assert transcribe_hints["source_requirements"]
    engine_ids = {engine["id"] for engine in transcribe_hints["engines"]}
    assert {
        "faster_whisper",
        "parakeet-tdt",
        "moss",
        "vibevoice-asr",
        "openai/whisper-1",
    } <= engine_ids
    # The deprecated `remote` placement id is gone from the
    # catalog entirely (deleted, not merely hidden).
    assert "remote" not in engine_ids

    answer = _catalog_entry(payload, "research.answer")
    answer_hints = answer["ui_hints"]
    # Typed research.answer renders through the generated form: the model is a
    # typed Param surfaced via ``semantic_controls`` (no ``uses_model`` flag),
    # and its outputs are the declared logical outputs.
    assert answer_hints["form"] == "generated"
    assert answer_hints["semantic_controls"]["model"] == "model"
    assert "model" in answer["input_schema"]["properties"]
    assert answer_hints["source_requirements"]
    assert answer_hints["logical_outputs"][0] == {
        "key": "answer",
        "column_type": "text",
    }
