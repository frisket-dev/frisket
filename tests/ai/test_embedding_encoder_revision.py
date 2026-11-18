"""Local embedding indexes follow the in-process encoder's generation.

An index embedded by an older FastEmbed generation must read as stale and be
re-embedded by an ordinary refresh, never searched or topped up with vectors
from the current encoder. Remote-provider indexes do not depend on the local
library and must stay current.
"""

from __future__ import annotations

import json

import pytest

from frisket.ai.embeddings import EmbeddingStore, build_batch_result, spaces
from frisket.ai.embeddings.freshness import (
    FRESHNESS_FRESH,
    FRESHNESS_STALE_ROWS,
    embedding_search_freshness_error,
    freshness_reason,
    index_freshness,
)
from frisket.ai.embeddings.spaces import LOCAL_FASTEMBED_ENCODER_REVISION
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.store import Project

_TEXTS = ("cat", "kitten", "airplane")


class RecordingGateway:
    def __init__(self, *, provider_kind: str, dim: int, offset: float):
        self.provider_kind = provider_kind
        self.dim = dim
        self.offset = offset
        self.calls: list[list[str]] = []

    def embed(self, texts, *, provider, model, modality):
        self.calls.append(list(texts))
        vectors = [
            [self.offset + _TEXTS.index(text)] + [0.0] * (self.dim - 1)
            for text in texts
        ]
        return build_batch_result(
            vectors,
            provider_id=provider,
            provider_kind=self.provider_kind,
            actual_model_id=model or "fake",
            modality=modality,
        )


@pytest.fixture
def project(make_project):
    project = make_project("revision")
    yield project


def _sheet(project: Project) -> int:
    sheet = project.add_sheet("animals")
    column = project.add_column(sheet, "headline")
    project.add_rows(
        sheet, [{"headline": text} for text in _TEXTS], {"headline": column}
    )
    return sheet


def _create(project: Project, sheet: int, *, provider: str) -> str:
    result = run_action_spec(
        project,
        {
            "action_id": "embedding.index_create",
            "scope": {"kind": "project"},
            "params": {
                "sheet_id": sheet,
                "source_columns": ["headline"],
                "modality": "text",
                "provider": provider,
                "source_policy": {"kind": "text_cell"},
                "maintenance_policy": {"mode": "manual"},
                "provider_policy": {"allow_remote": provider != "fastembed"},
            },
            "idempotency_key": f"revision_create@{provider}",
        },
        project_id="p",
    )
    assert result.status == "completed", result.errors
    return result.outputs[0].ref["index_id"]


def _refresh(project: Project, index_id: str, gateway, *, key: str) -> None:
    result = run_action_spec(
        project,
        {
            "action_id": "embedding.index_refresh",
            "scope": {"kind": "project"},
            "params": {"index_id": index_id, "mode": "incremental"},
            "idempotency_key": f"revision_refresh@{key}",
        },
        project_id="p",
        deps=ExecutorDeps(embedding_gateway=gateway),
    )
    assert result.status == "completed", result.errors


def _space(project: Project, index_id: str):
    store = EmbeddingStore(project)
    index = store.get_index(index_id)
    space = store.get_space(index["space_id"])
    return index, space, json.loads(space["descriptor_json"])


def _reason(project: Project, index_id: str) -> str:
    index = EmbeddingStore(project).get_index(index_id)
    return freshness_reason(index, index_freshness(project, index))


def test_older_local_encoder_index_is_stale_and_refresh_reembeds_it(
    project, monkeypatch
):
    sheet = _sheet(project)
    old = RecordingGateway(provider_kind="local_process", dim=384, offset=10.0)
    with monkeypatch.context() as patched:
        # The space descriptor a local index carried before encoder generations.
        patched.setattr(spaces, "LOCAL_FASTEMBED_ENCODER_REVISION", "unknown")
        index_id = _create(project, sheet, provider="fastembed")
        _refresh(project, index_id, old, key="old")
        assert _reason(project, index_id) == FRESHNESS_FRESH
    _index, old_space, old_descriptor = _space(project, index_id)
    assert old_descriptor["model_revision"] == "unknown"

    assert _reason(project, index_id) == FRESHNESS_STALE_ROWS
    gate = embedding_search_freshness_error(project, index_id)
    assert gate is not None and gate[0] == "embedding_source_stale"

    current = RecordingGateway(provider_kind="local_process", dim=384, offset=20.0)
    _refresh(project, index_id, current, key="current")

    assert sorted(text for call in current.calls for text in call) == sorted(_TEXTS)
    _index, new_space, new_descriptor = _space(project, index_id)
    assert new_space["id"] != old_space["id"]
    assert new_descriptor["model_revision"] == LOCAL_FASTEMBED_ENCODER_REVISION
    assert {**new_descriptor, "model_revision": "unknown"} == old_descriptor
    assert _reason(project, index_id) == FRESHNESS_FRESH
    assert embedding_search_freshness_error(project, index_id) is None

    with monkeypatch.context() as patched:
        patched.setattr(spaces, "LOCAL_FASTEMBED_ENCODER_REVISION", "unknown")
        assert _reason(project, index_id) == FRESHNESS_STALE_ROWS


def test_remote_provider_index_is_not_invalidated_by_the_local_encoder(project):
    sheet = _sheet(project)
    gateway = RecordingGateway(provider_kind="platform_api", dim=1536, offset=1.0)
    index_id = _create(project, sheet, provider="openai")
    _refresh(project, index_id, gateway, key="first")
    _index, space, descriptor = _space(project, index_id)
    assert descriptor["model_revision"] == "unknown"
    assert spaces.current_space_descriptor(descriptor) is descriptor

    assert _reason(project, index_id) == FRESHNESS_FRESH
    assert EmbeddingStore(project).move_index_to_current_space(index_id) is None
    gateway.calls.clear()
    _refresh(project, index_id, gateway, key="second")

    assert gateway.calls == []
    _index, after, _descriptor = _space(project, index_id)
    assert after["id"] == space["id"]
    assert _reason(project, index_id) == FRESHNESS_FRESH
