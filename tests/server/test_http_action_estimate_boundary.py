from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import media_cell, owned_media_metadata_document
from frisket.execution.pricing_policy import (
    QuoteFacts,
    RatedQuote,
    install_pricing_policy,
)
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.server.app import create_app
from frisket.server.services.action_previews import ActionPreviewService


def _client(tmp_path) -> TestClient:
    router = ModelRouter(keys={"gemini": "k"}, cache=None, cache_mode="off")
    return TestClient(create_app(tmp_path / "workspace", router=router))


def _seed(client: TestClient) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "V1 Estimate"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("stories")
    cols = {"snippet": project.add_column(sheet, "snippet", type="text")}
    project.add_rows(
        sheet,
        [
            {"snippet": "The mayor awarded a paving contract without bids."},
            {"snippet": "The light rail extension opened downtown."},
        ],
        cols,
    )
    return pid, sheet


def _classify_action(sheet: int) -> dict:
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {
            "source": ["snippet"],
            "engine": "llm",
            "model": "gemini/gemini-2.5-flash",
            "context": "Classify each local news snippet into exactly one beat.",
            "fields": [
                {
                    "name": "beat",
                    "type": "category",
                    "labels": ["accountability", "transit", "other"],
                    "description": "Local news beat.",
                }
            ],
            "include_justification": True,
            "include_confidence": True,
        },
        "idempotency_key": "estimate-classify@sha256:test",
    }


def _extract_action(sheet: int) -> dict:
    return {
        "action_id": "map.extract",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {
            "source": ["snippet"],
            "model": "gemini/gemini-2.5-flash",
            "instruction": "Extract the primary public body mentioned.",
            "context": "Rows are local news snippets.",
            "fields": [
                {
                    "name": "public_body",
                    "type": "text",
                    "description": "Primary public body mentioned in the row.",
                }
            ],
            "include_confidence": True,
        },
        "idempotency_key": "estimate-extract@sha256:test",
    }


def _summarize_action(sheet: int) -> dict:
    return {
        "action_id": "map.summarize",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {
            "source": ["snippet"],
            "model": "gemini/gemini-3.5-flash",
            "preset": "one_line",
            "instruction": "Summarize each local news snippet in one sentence.",
            "context": "Rows are local news snippets.",
        },
        "output_names": {"summary": "summary"},
        "idempotency_key": "estimate-summarize@sha256:test",
    }


def _find_action(sheet: int) -> dict:
    return {
        "action_id": "map.find",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "sheet_name": "Findings",
        "params": {
            "source": "snippet",
            "instruction": "Find every discussion of public contracts.",
            "fields": [],
            "model": "anthropic/claude-haiku-4-5",
        },
        "idempotency_key": "estimate-find@sha256:test",
    }


def _ner_action(
    sheet: int,
    *,
    engine: str,
    row_ids: list[int] | None = None,
) -> dict:
    params = {
        "source": ["snippet"],
        "labels": ["person", "organization", "location"],
        "threshold": 0.5,
        "engine": engine,
    }
    if engine == "llm":
        params["model"] = "gemini/gemini-2.5-flash"
    return {
        "action_id": "map.ner",
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": sheet,
            "row_ids": row_ids,
        },
        "output_names": {"entities": "entities"},
        "params": params,
        "idempotency_key": f"estimate-ner-{engine}@sha256:test",
    }


def test_v1_action_estimate_uses_action_spec_without_creating_run_state(
    tmp_path,
) -> None:
    client = _client(tmp_path)
    pid, sheet = _seed(client)
    project = client.app.state.workspace.get(pid)
    before_columns = [
        (c["name"], c["type"], c["current_run_id"]) for c in project.columns(sheet)
    ]

    resp = client.post(
        f"/api/projects/{pid}/actions/v1/estimate",
        json={"action": _classify_action(sheet)},
    )

    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["schema_version"] == "frisket.action_estimate_result.v1"
    assert out["action"]["kind"] == "map.classify"
    assert out["project_id"] == pid
    assert out["estimate"]["rows"] == 2
    assert "cost" in out["estimate"]

    after_columns = [
        (c["name"], c["type"], c["current_run_id"]) for c in project.columns(sheet)
    ]
    assert after_columns == before_columns
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["map.find", "reduce.group_summary"])
def test_v1_action_estimate_supports_prepared_models_without_creating_run_state(
    tmp_path,
    kind,
) -> None:
    client = _client(tmp_path)
    pid, sheet = _seed(client)
    project = client.app.state.workspace.get(pid)

    request = _find_action(sheet)
    if kind == "reduce.group_summary":
        request["action_id"] = kind
        request["params"]["source"] = ["snippet"]
        del request["params"]["fields"]
    response = client.post(
        f"/api/projects/{pid}/actions/v1/estimate",
        json={"action": request},
    )

    assert response.status_code == 200, response.text
    out = response.json()
    assert out["action"] == {"kind": kind}
    assert out["estimate"]["rows"] == 2
    assert out["estimate"]["cost"] is not None
    assert out["estimate"]["cost_source"] != "unknown"
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 1


def test_v1_action_estimate_uses_the_typed_executor_plan(tmp_path, monkeypatch) -> None:
    from frisket.server.services import action_previews

    client = _client(tmp_path)
    project_id, sheet = _seed(client)
    project = client.app.state.workspace.get(project_id)
    request = _classify_action(sheet)
    request["params"]["source"] = {"text": "Story: {{snippet}}"}
    request["scope"]["row_ids"] = [project.visible_row_ids(sheet)[0]]
    prepared = []
    build = action_previews.build_typed_map_rows_plan

    def capture_plan(selected, bound, **options):
        plan = build(selected, bound, **options)
        prepared.append((bound, options, plan))
        return plan

    monkeypatch.setattr(action_previews, "build_typed_map_rows_plan", capture_plan)
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/estimate", json={"action": request}
    )
    assert response.status_code == 200, response.text
    assert response.json()["estimate"]["rows"] == 1
    assert len(prepared) == 1
    bound, options, plan = prepared[0]
    assert bound.params.source.text == "Story: {{snippet}}"
    assert bound.request.scope.row_ids == tuple(request["scope"]["row_ids"])
    assert options == {"_admit_output_targets": False}
    assert plan.program is not None
    assert plan.spec_dict()["action_kind"] == "map.classify"


def test_v1_action_estimate_supports_map_extract_action_spec(tmp_path) -> None:
    client = _client(tmp_path)
    pid, sheet = _seed(client)
    project = client.app.state.workspace.get(pid)
    before_columns = [
        (c["name"], c["type"], c["current_run_id"]) for c in project.columns(sheet)
    ]

    resp = client.post(
        f"/api/projects/{pid}/actions/v1/estimate",
        json={"action": _extract_action(sheet)},
    )

    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["schema_version"] == "frisket.action_estimate_result.v1"
    assert out["action"]["kind"] == "map.extract"
    assert out["project_id"] == pid
    assert out["estimate"]["rows"] == 2
    assert "cost" in out["estimate"]

    after_columns = [
        (c["name"], c["type"], c["current_run_id"]) for c in project.columns(sheet)
    ]
    assert after_columns == before_columns
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0


def test_v1_action_estimate_supports_map_summarize_action_spec(tmp_path) -> None:
    client = _client(tmp_path)
    pid, sheet = _seed(client)
    project = client.app.state.workspace.get(pid)
    before_columns = [
        (c["name"], c["type"], c["current_run_id"]) for c in project.columns(sheet)
    ]

    resp = client.post(
        f"/api/projects/{pid}/actions/v1/estimate",
        json={"action": _summarize_action(sheet)},
    )

    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["schema_version"] == "frisket.action_estimate_result.v1"
    assert out["action"]["kind"] == "map.summarize"
    assert out["project_id"] == pid
    assert out["estimate"]["rows"] == 2
    assert out["estimate"]["cost"] is not None
    assert out["estimate"]["cost_source"] != "unknown"

    after_columns = [
        (c["name"], c["type"], c["current_run_id"]) for c in project.columns(sheet)
    ]
    assert after_columns == before_columns
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0


def test_v1_action_estimate_is_engine_aware_for_map_ner(tmp_path) -> None:
    client = _client(tmp_path)
    pid, sheet = _seed(client)
    project = client.app.state.workspace.get(pid)

    local = client.post(
        f"/api/projects/{pid}/actions/v1/estimate",
        json={"action": _ner_action(sheet, engine="spacy")},
    )
    llm = client.post(
        f"/api/projects/{pid}/actions/v1/estimate",
        json={"action": _ner_action(sheet, engine="llm")},
    )

    assert local.status_code == 200, local.text
    local_out = local.json()
    assert local_out["action"]["kind"] == "map.ner"
    assert local_out["estimate"]["rows"] == 2
    assert local_out["estimate"]["cost"] == 0
    assert llm.status_code == 200, llm.text
    llm_out = llm.json()
    assert llm_out["action"]["kind"] == "map.ner"
    assert llm_out["estimate"]["rows"] == 2
    assert "cost" in llm_out["estimate"]
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0


def test_v1_action_estimate_refuses_invalid_exact_ner_membership(tmp_path) -> None:
    client = _client(tmp_path)
    pid, sheet = _seed(client)
    project = client.app.state.workspace.get(pid)
    valid_row = project.visible_row_ids(sheet)[0]

    response = client.post(
        f"/api/projects/{pid}/actions/v1/estimate",
        json={
            "action": _ner_action(
                sheet,
                engine="spacy",
                row_ids=[valid_row, 999_999],
            )
        },
    )

    assert response.status_code == 400, response.text
    assert response.json() == {"detail": "row_ids must belong to the target sheet"}


def test_typed_python_estimate_is_free_without_running_or_materializing(
    tmp_path,
) -> None:
    client = _client(tmp_path)
    pid, sheet = _seed(client)
    action = {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {
            "input_columns": ["snippet"],
            "code": "raise AssertionError('estimate must not execute Python')",
            "return_schema": {"type": "string"},
            "output_routes": [
                {
                    "name": "match",
                    "path": "$",
                    "target": {
                        "kind": "column",
                        "type": "text",
                    },
                }
            ],
        },
        "output_names": {"match": "match"},
        "idempotency_key": "estimate-local-python@sha256:stable",
    }

    resp = client.post(
        f"/api/projects/{pid}/actions/v1/estimate",
        json={"action": action},
    )

    assert resp.status_code == 200, resp.text
    estimate = resp.json()["estimate"]
    assert estimate["rows"] == 2
    assert estimate["cost"] == 0
    assert estimate["cost_source"] == "free_local"
    assert estimate["billed_cost"] == 0
    project = client.app.state.workspace.get(pid)
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    assert [column["name"] for column in project.columns(sheet)] == ["snippet"]


def _seed_audio(client: TestClient) -> tuple[str, int]:
    """One row of audio with known duration metadata — the input the recipe's
    estimate reads to price per SECOND rather than per row."""
    pid = client.post("/api/projects", json={"name": "Audio Estimate"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("audio")
    cols = {"media": project.add_column(sheet, "media", type="audio")}
    blob = project.add_blob(
        b"RIFFxxxxWAVEfmt ",
        filename="clip.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"kind": "audio", "duration_seconds": 10.64}
        ),
    )
    project.add_rows(
        sheet,
        [{"media": media_cell(blob, mime="audio/wav", filename="clip.wav")}],
        cols,
    )
    return pid, sheet


def _seed_ocr(client: TestClient) -> tuple[str, int]:
    pid = client.post("/api/projects", json={"name": "OCR Estimate"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet = project.add_sheet("documents")
    cols = {"media": project.add_column(sheet, "media", type="file")}
    blob = project.add_blob(
        b"%PDF-1.4\n%%EOF",
        filename="scan.pdf",
        mime="application/pdf",
        metadata=owned_media_metadata_document(probe={"kind": "pdf", "pages": 3}),
    )
    project.add_rows(
        sheet,
        [{"media": media_cell(blob, mime="application/pdf", filename="scan.pdf")}],
        cols,
    )
    return pid, sheet


def test_v1_action_estimate_supports_local_ocr_without_creating_run_state(
    tmp_path,
) -> None:
    client = _client(tmp_path)
    pid, sheet = _seed_ocr(client)
    project = client.app.state.workspace.get(pid)

    response = client.post(
        f"/api/projects/{pid}/actions/v1/estimate",
        json={
            "action": {
                "action_id": "media.ocr",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {"source": "media", "engine": "rapidocr"},
                "output_names": {"text": "ocr_text"},
                "idempotency_key": "estimate-ocr@sha256:test",
            }
        },
    )

    assert response.status_code == 200, response.text
    out = response.json()
    assert out["action"] == {"kind": "media.ocr"}
    assert out["estimate"]["rows"] == 1
    assert out["estimate"]["cost"] == 0
    assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0


@pytest.mark.parametrize("configured", (False, True))
def test_v1_action_estimate_requires_docling_route_before_zero_quote(
    tmp_path, monkeypatch, configured
) -> None:
    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)
    if configured:
        monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.invalid")
        monkeypatch.setenv("FRISKET_MODELS_TOKEN", "test-token")
    client = _client(tmp_path)
    pid, sheet = _seed_ocr(client)

    response = client.post(
        f"/api/projects/{pid}/actions/v1/estimate",
        json={
            "action": {
                "action_id": "media.to_markdown",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {"source": "media", "engine": "docling"},
                "output_names": {"markdown": "markdown"},
                "idempotency_key": "estimate-docling@sha256:test",
            }
        },
    )

    if configured:
        assert response.status_code == 200, response.text
        out = response.json()
        assert out["action"] == {"kind": "media.to_markdown"}
        assert out["estimate"]["cost"] == 0
        assert out["estimate"]["cost_source"] == "free_local"
    else:
        assert response.status_code == 400, response.text
        assert "FRISKET_MODELS_URL" in response.text
    project = client.app.state.workspace.get(pid)
    for table in ("runs", "results", "model_calls"):
        assert project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_v1_action_estimate_prices_transcription_in_audio_seconds(
    tmp_path, monkeypatch
) -> None:
    """The transcribe panel said "UNKNOWN — no published per-row price" while
    the server's own cost gate quoted $0.001064. It said so because the panel
    had nowhere to ask: /estimate refused media.transcribe outright, so the
    only priced answer arrived as a 402 AFTER launch.

    whisper-1 is $0.0001 per audio second (pricing_data.json), so 10.64s is
    $0.001064 — a real figure, in the unit the SKU is quoted in."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    client = _client(tmp_path)
    pid, sheet = _seed_audio(client)

    resp = client.post(
        f"/api/projects/{pid}/actions/v1/estimate",
        json={
            "action": {
                "action_id": "media.transcribe",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {"source": "media", "engine": "openai/whisper-1"},
                "output_names": {"text": "transcript"},
                "idempotency_key": "estimate-transcribe@sha256:test",
            }
        },
    )

    assert resp.status_code == 200, resp.text
    estimate = resp.json()["estimate"]
    assert estimate["cost"] == 0.001064
    assert estimate["audio_seconds"] == 10.64
    assert estimate["rows"] == 1


class _CountingCostPlusPolicy:
    policy_id = "test.preview.cost-plus.v1"

    def __init__(self) -> None:
        self.facts: list[QuoteFacts] = []

    def rate(self, facts: QuoteFacts) -> RatedQuote:
        self.facts.append(facts)
        assert facts.provider_cost is not None
        return RatedQuote(
            billed_cost=facts.provider_cost * 2,
            provider_cost=facts.provider_cost,
            lane="cost_plus",
            policy_id=self.policy_id,
        )


def test_routed_action_preview_rates_once_and_returns_that_billed_quote(
    tmp_path,
) -> None:
    """Cross-seam pin for the routed producer and preview-service consumer.

    ``MapRunner.estimate`` rates a routed estimate before binding that quote
    into its claims hash. The preview service must forward that current shape,
    not feed it back through the policy a second time.
    """
    project = Project.create(tmp_path / "preview.frisket")
    sheet = project.add_sheet("audio")
    column = project.add_column(sheet, "media", type="audio")
    blob = project.add_blob(
        b"RIFFxxxxWAVEfmt ",
        filename="clip.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 10.64, "kind": "audio"}
        ),
    )
    project.add_rows(
        sheet,
        [{"media": media_cell(blob, mime="audio/wav", filename="clip.wav")}],
        {"media": column},
    )
    router = ModelRouter(keys={"openai": "k"}, cache=None, cache_mode="off")
    composition = open_execution_composition(
        project, router, ExecutionCompositionContext.direct()
    )

    class _PreviewWorkspace:
        edition = "solo"
        executor_deps_factory = None

        def get(self, project_id: str) -> Project:
            assert project_id == "project"
            return project

        def router_for(self, selected: Project) -> ModelRouter:
            assert selected is project
            return router

        def execution_composition_for(
            self,
            selected: Project,
            effective_router: ModelRouter,
            _context: ExecutionCompositionContext,
        ):
            assert selected is project
            assert effective_router is router
            return composition

        @staticmethod
        def edition_execution_composition_context_for(
            _request_context=None,
        ) -> ExecutionCompositionContext:
            return ExecutionCompositionContext.direct()

    policy = _CountingCostPlusPolicy()
    install_pricing_policy(policy)
    try:
        result = ActionPreviewService(_PreviewWorkspace()).estimate(
            "project",
            {
                "action_id": "media.transcribe",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {"source": "media", "engine": "openai/whisper-1"},
                "output_names": {"text": "transcript"},
                "idempotency_key": "estimate-transcribe-once@sha256:test",
            },
        )
        estimate = result["estimate"]
        assert estimate["policy_id"] == policy.policy_id
        assert estimate["cost"] == 0.001064
        assert estimate["billed_cost"] == 2_128
        assert estimate["requires_confirmation"] is False
        assert estimate["venue_label"] == "Media is sent to openai, a third-party API."
        assert (
            estimate["billing_label"]
            == "billed to your own account; no platform charge"
        )
        assert len(policy.facts) == 1
        assert policy.facts[0].provider_cost == 1_064
    finally:
        project.close()
