from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.ai.map.find_source import AddressedUnit
from frisket.ai.vision.region_locator import prepare_image_asset
from frisket.actions.types import ActionRequest
from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.find_plan import FindOperation
from frisket.actions.find_types import FindOptions
from frisket.engine.executor.action_bindings import action_job_bindings
from frisket.engine.executor.action_jobs import (
    ActionJobEnvelope,
    run_action_run_job,
)
from frisket.engine.executor.action_inventory import ExecutorDeps
from frisket.engine.executor.map_find_planning import (
    estimate_find_scan_cost,
    plan_find_scan,
)
from frisket.engine.executor.map_find_scan import (
    FindMatch,
    VisualFindMatch,
    scan_find_plans,
)
from frisket.engine.executor.map_find_source import (
    ResolvedFindSource,
    resolve_find_sources,
)
from frisket.engine.store import Project
from frisket.engine.store.artifact_timeline import resolve_timeline
from frisket.engine.store.evidence import (
    TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION,
    list_row_evidence,
    record_evidence_link,
    record_source_artifact,
    record_source_span,
    resolve_evidence_viewer,
)
from frisket.engine.store.media_blobs import owned_media_metadata_document
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.runs import RunResultStore
from helpers import replace_test_source_cell, write_claimed_test_results


def _operation_for_request(request):
    options = FindOptions.model_validate(
        {key: value for key, value in request.params.items() if key != "source"}
    )
    return FindOperation(
        action_kind=request.action_id,
        request=request.model_dump(mode="json", exclude={"confirmation"}),
        sheet_id=request.scope.sheet_id,
        row_ids=request.scope.row_ids,
        source_column=request.params["source"],
        model=options.model.root,
        instruction=options.instruction,
        fields=options.fields,
        target_sheet_name=request.sheet_name,
        output_names={
            "match": request.output_names.get("match", "match"),
            **{
                field.name: request.output_names.get(field.name, field.name)
                for field in options.fields
            },
        },
    )


def _queued_find(project, request, *, project_id):
    from frisket.contracts.action import ActionResult
    from frisket.engine.executor.find_action import reserve_typed_find_action_job

    bound = typed_action_for_request(request.model_dump(mode="json"))
    result = reserve_typed_find_action_job(project, project_id, bound)
    if isinstance(result, ActionResult):
        assert result.status == "needs_confirmation", result.model_dump()
        request = request.model_copy(
            update={"confirmation": result.errors[0].details["promise_set_hash"]}
        )
        result = reserve_typed_find_action_job(
            project,
            project_id,
            typed_action_for_request(request.model_dump(mode="json")),
        )
    assert isinstance(result, ActionJobEnvelope), result
    return result


def _run_queued_find(project, envelope, adapter):
    router = ModelRouter(keys={"anthropic": "test"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    bindings = action_job_bindings(
        executor_deps_factory=lambda _project_id, _run_id: ExecutorDeps(router=router)
    )
    return run_action_run_job(
        project,
        {"action_job": envelope.to_json(), "job_id": 7},
        executor_lookup=bindings.get,
    )


class _FindAdapter:
    def __init__(
        self,
        match_unit_id: str,
        *,
        include_uncited: bool = False,
        matches: list[dict[str, Any]] | None = None,
        matches_by_call: list[list[dict[str, Any]]] | None = None,
    ) -> None:
        self.match_unit_id = match_unit_id
        self.include_uncited = include_uncited
        self.matches = matches
        self.matches_by_call = matches_by_call
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client: Any) -> LLMResponse:
        del client
        call_index = len(self.requests)
        self.requests.append(req)
        matches = (
            self.matches_by_call[call_index]
            if self.matches_by_call is not None
            else self.matches
        )
        data = {
            "matches": list(matches)
            if matches is not None
            else [
                {
                    "match": "China trade policy",
                    "source_unit_ids": [self.match_unit_id],
                    "details": {"sentiment": "neutral"},
                }
            ]
        }
        if self.include_uncited:
            data["matches"].append(
                {
                    "match": "Unsupported sibling",
                    "source_unit_ids": [],
                    "details": {},
                }
            )
        return LLMResponse(
            content=json.dumps(data),
            data=data,
            tokens_in=71,
            tokens_out=19,
            cost=0.0004,
            model=req.model,
            provider="anthropic",
            credential_source="project",
            cost_source="provider_reported",
            duration_ms=8,
        )


@pytest.mark.parametrize("local", [False, True])
def test_queued_find_network_off_allows_only_configured_local_model(tmp_path, local):
    from frisket.ai.llm.adapters import OpenAICompatAdapter
    from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig

    class Adapter(_FindAdapter, OpenAICompatAdapter):
        def __init__(self):
            _FindAdapter.__init__(self, "unused", matches=[])
            OpenAICompatAdapter.__init__(self, "test", "http://127.0.0.1:11434/v1")

        async def complete(self, req, client, *, cost_model=None):
            del cost_model
            return await super().complete(req, client)

    adapter = Adapter()
    router = ModelRouter(
        keys={"anthropic": "stub"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
        local_endpoints=(
            LocalModelEndpointConfig(
                endpoint_id="test-local",
                display_name="Test local",
                origin="http://127.0.0.1:11434",
                source="local_file",
            ),
        ),
    )
    router._adapters["anthropic"] = adapter
    router._adapters["ollama"]._adapters["test-local"] = adapter
    project = Project.create(tmp_path / "network-off-find.frisket")
    try:
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "body", type="text")
        project.add_rows(sheet, [{"body": "Local policy report."}], {"body": column})
        project.set_network_policy(mode="off")
        request = ActionRequest(
            action_id="map.find",
            scope={"kind": "sheet_rows", "sheet_id": sheet},
            params={
                "source": "body",
                "model": "ollama/@test-local/llama3"
                if local
                else "anthropic/claude-haiku-4-5",
                "instruction": "Find policy mentions.",
            },
            sheet_name="Findings",
            idempotency_key="network-off-find",
        )
        envelope = _queued_find(project, request, project_id="network-off-find")
        bindings = action_job_bindings(
            executor_deps_factory=lambda _project_id, _run_id: ExecutorDeps(
                router=router
            )
        )
        result = run_action_run_job(
            project,
            {"action_job": envelope.to_json(), "job_id": 7},
            executor_lookup=bindings.get,
        )
        assert result.status == ("completed" if local else "failed"), (
            result.model_dump()
        )
        assert len(adapter.requests) == int(local)
        if local:
            calls = RunResultStore(project).model_calls(result.run_id)
            assert len(calls) == 1
            assert calls[0]["cost_source"] == "free_local"
        else:
            assert result.errors[0].code == "network_disabled"
    finally:
        project.close()


class _VisualFindAdapter:
    def __init__(self, matches: list[dict[str, Any]] | None = None) -> None:
        self.matches = matches
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client: Any) -> LLMResponse:
        del client
        self.requests.append(req)
        data = {
            "matches": list(self.matches)
            if self.matches is not None
            else [
                {
                    "match": "blue rectangle",
                    "bbox_pixels_xyxy": [10, 12, 50, 42],
                    "details": {"sentiment": "neutral"},
                }
            ]
        }
        return LLMResponse(
            content=json.dumps(data),
            data=data,
            tokens_in=80,
            tokens_out=24,
            cost=0.0005,
            model=req.model,
            provider="anthropic",
            credential_source="project",
            cost_source="provider_reported",
            duration_ms=8,
        )


def _seed_timestamped_video(project: Project) -> tuple[int, int]:
    sheet_id = project.add_sheet("Video")
    media_column_id = project.add_column(sheet_id, "video", "video")
    blob_hash = project.add_blob(
        b"video bytes",
        filename="source.mp4",
        mime="video/mp4",
        metadata=owned_media_metadata_document(probe={"duration_seconds": 12.0}),
    )
    row_id = project.add_rows(
        sheet_id,
        [
            {
                "video": {
                    "blob": blob_hash,
                    "filename": "source.mp4",
                    "mime": "video/mp4",
                }
            }
        ],
        {"video": media_column_id},
    )[0]
    timeline = resolve_timeline(
        project,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=media_column_id,
    )
    transcript_column_id = project.add_column(
        sheet_id, "transcript", "timestamped_transcript", ai_generated=True
    )
    op_id = project.append_op(
        "media.transcribe", {"kind": "media.transcribe", "params": {}}
    )
    runs = RunResultStore(project)
    run_id = runs.start_run(
        op_id,
        sheet_id,
        "media.transcribe",
        total_rows=1,
        row_ids=[row_id],
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": transcript_column_id,
                "value": "Opening. China trade policy. Closing.",
            }
        ],
    )
    runs.finish_run(run_id)
    runs.point_column_at_run(op_id, transcript_column_id, run_id)
    spans = []
    for index, (start_ms, end_ms, quote) in enumerate(
        [
            (0, 2_000, "Opening."),
            (2_000, 7_000, "China trade policy."),
            (7_000, 10_000, "Closing."),
        ]
    ):
        span = record_source_span(
            project,
            artifact_id=timeline.anchor.artifact_id,
            span_kind="temporal",
            start_ms=start_ms,
            end_ms=end_ms,
            quote=quote,
            selector={"segment_index": index},
        )
        spans.append({"span_id": span["id"], "rank": index})
    _values, refs = project.get_values_with_refs(
        sheet_id, transcript_column_id, row_ids=[row_id]
    )
    record_evidence_link(
        project,
        subject_kind="cell",
        subject_ref=refs[row_id],
        spans=spans,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=transcript_column_id,
        run_id=run_id,
        op_id=op_id,
        link_role="media_transcribe_temporal",
        producer={"action_kind": "media.transcribe"},
        metadata={
            "schema_version": TIMESTAMPED_TRANSCRIPT_EVIDENCE_SCHEMA_VERSION,
            "semantic_type": "timestamped_transcript",
            "language": "en",
        },
    )
    return sheet_id, row_id


def test_scan_locates_distinct_occurrences_and_dedupes_exact_locations(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "find-order.frisket", name="Find order")
    try:
        sheet_id = project.add_sheet("Sources")
        column_id = project.add_column(sheet_id, "body", "text")
        row_id = project.add_rows(
            sheet_id,
            [{"body": "China first. Then China later."}],
            {"body": column_id},
        )[0]
        params = _operation_for_request(
            ActionRequest(
                action_id="map.find",
                scope={"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": [row_id]},
                sheet_name="Findings",
                output_names={"match": "Match"},
                idempotency_key="find-source-test",
                params={
                    "source": "body",
                    "instruction": "Find every mention of China.",
                    "model": "anthropic/claude-haiku-4-5",
                },
            )
        )
        sources = resolve_find_sources(project, params)
        assert isinstance(sources, tuple)
        assert len(sources[0].units) == 1
        unit = sources[0].units[0]
        adapter = _FindAdapter(
            str(unit.unit_id),
            matches=[
                {
                    "match": "China later",
                    "source_unit_ids": [unit.unit_id],
                    "details": {},
                },
                {
                    "match": "China first",
                    "source_unit_ids": [unit.unit_id],
                    "details": {},
                },
                {
                    "match": "China first",
                    "source_unit_ids": [unit.unit_id],
                    "details": {},
                },
            ],
        )
        router = ModelRouter(keys={"anthropic": "test"}, cache=None, cache_mode="off")
        router._adapters["anthropic"] = adapter  # noqa: SLF001
        plans = plan_find_scan(params, sources)

        scan = asyncio.run(scan_find_plans(params, plans, router=router))
        estimate = estimate_find_scan_cost(params, plans)

        assert [match.candidate.match for match in scan.matches] == [
            "China first",
            "China later",
        ]
        assert scan.matches[0].span_ids != scan.matches[1].span_ids
        assert all(span_id < 0 for match in scan.matches for span_id in match.span_ids)
        assert scan.issues == ()
        assert estimate["execution_max_output_tokens_per_window"] == 32_768
        assert estimate["rows"] == 1
        assert estimate["estimated_output_tokens"] == 4_096
        assert estimate["estimated_output_tokens_per_source_row"] == 4_096
        assert estimate["repair_attempts"] == 1
    finally:
        project.close()


def test_scan_keeps_distinct_occurrences_sharing_one_positive_span() -> None:
    source = ResolvedFindSource(
        sheet_id=1,
        row_id=2,
        column_id=3,
        column_type="timestamped_transcript",
        source_snapshot="snapshot",
        value_ref={},
        artifact_id=4,
        units=(
            AddressedUnit(
                unit_id="segment-0",
                text="Alice laughed. Bob cried.",
                span_ids=(42,),
            ),
        ),
    )
    params = _operation_for_request(
        ActionRequest(
            action_id="map.find",
            scope={"kind": "sheet_rows", "sheet_id": source.sheet_id},
            sheet_name="Findings",
            output_names={"match": "Match"},
            idempotency_key="find-source-test",
            params={
                "source": "transcript",
                "instruction": "Find every emotional reaction.",
                "model": "anthropic/claude-haiku-4-5",
            },
        )
    )
    adapter = _FindAdapter(
        "segment-0",
        matches=[
            {
                "match": "Bob cried",
                "source_unit_ids": ["segment-0"],
                "details": {},
            },
            {
                "match": "Alice laughed",
                "source_unit_ids": ["segment-0"],
                "details": {},
            },
            {
                "match": "Alice laughed",
                "source_unit_ids": ["segment-0"],
                "details": {},
            },
        ],
    )
    router = ModelRouter(keys={"anthropic": "test"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter  # noqa: SLF001

    scan = asyncio.run(
        scan_find_plans(params, plan_find_scan(params, (source,)), router=router)
    )

    assert [match.candidate.match for match in scan.matches] == [
        "Alice laughed",
        "Bob cried",
    ]
    assert [match.span_ids for match in scan.matches] == [(42,), (42,)]
    assert scan.issues == ()


def test_scan_orders_multi_unit_citations_by_resolved_match_position() -> None:
    source = ResolvedFindSource(
        sheet_id=1,
        row_id=2,
        column_id=3,
        column_type="timestamped_transcript",
        source_snapshot="snapshot",
        value_ref={},
        artifact_id=4,
        units=(
            AddressedUnit(
                unit_id="segment-0",
                text="xxxxxxxxxxxxxxxxxxxxEARLY",
                span_ids=(41,),
            ),
            AddressedUnit(unit_id="segment-1", text="LATE", span_ids=(42,)),
        ),
    )
    params = _operation_for_request(
        ActionRequest(
            action_id="map.find",
            scope={"kind": "sheet_rows", "sheet_id": source.sheet_id},
            sheet_name="Findings",
            output_names={"match": "Match"},
            idempotency_key="find-source-test",
            params={
                "source": "transcript",
                "instruction": "Find both occurrences.",
                "model": "anthropic/claude-haiku-4-5",
            },
        )
    )
    adapter = _FindAdapter(
        "segment-0",
        matches=[
            {
                "match": "EARLY",
                "source_unit_ids": ["segment-0"],
                "details": {},
            },
            {
                "match": "LATE",
                "source_unit_ids": ["segment-0", "segment-1"],
                "details": {},
            },
        ],
    )
    router = ModelRouter(keys={"anthropic": "test"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter  # noqa: SLF001

    scan = asyncio.run(
        scan_find_plans(params, plan_find_scan(params, (source,)), router=router)
    )

    assert [match.candidate.match for match in scan.matches] == ["EARLY", "LATE"]
    assert scan.issues == ()


def test_scan_exact_duplicate_retains_earliest_cited_evidence() -> None:
    source = ResolvedFindSource(
        sheet_id=1,
        row_id=2,
        column_id=3,
        column_type="timestamped_transcript",
        source_snapshot="snapshot",
        value_ref={},
        artifact_id=4,
        units=(
            AddressedUnit(unit_id="segment-0", text="context", span_ids=(41,)),
            AddressedUnit(unit_id="segment-1", text="LATE", span_ids=(42,)),
        ),
    )
    params = _operation_for_request(
        ActionRequest(
            action_id="map.find",
            scope={"kind": "sheet_rows", "sheet_id": source.sheet_id},
            sheet_name="Findings",
            output_names={"match": "Match"},
            idempotency_key="find-source-test",
            params={
                "source": "transcript",
                "instruction": "Find the occurrence.",
                "model": "anthropic/claude-haiku-4-5",
            },
        )
    )
    adapter = _FindAdapter(
        "segment-0",
        matches=[
            {
                "match": "LATE",
                "source_unit_ids": ["segment-1"],
                "details": {},
            },
            {
                "match": "LATE",
                "source_unit_ids": ["segment-0", "segment-1"],
                "details": {},
            },
        ],
    )
    router = ModelRouter(keys={"anthropic": "test"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter  # noqa: SLF001

    scan = asyncio.run(
        scan_find_plans(params, plan_find_scan(params, (source,)), router=router)
    )

    assert len(scan.matches) == 1
    match = scan.matches[0]
    assert isinstance(match, FindMatch)
    assert match.candidate.source_unit_ids == ("segment-0", "segment-1")
    assert match.span_ids == (41, 42)
    assert scan.issues == ()


def test_scan_globally_orders_matches_across_overlap_windows() -> None:
    units = tuple(
        AddressedUnit(
            unit_id=f"segment-{index}",
            text=("MIDDLE" if index == 24 else "LATE" if index == 25 else "context"),
            span_ids=(100 + index,),
        )
        for index in range(26)
    )
    source = ResolvedFindSource(
        sheet_id=1,
        row_id=2,
        column_id=3,
        column_type="timestamped_transcript",
        source_snapshot="snapshot",
        value_ref={},
        artifact_id=4,
        units=units,
    )
    params = _operation_for_request(
        ActionRequest(
            action_id="map.find",
            scope={"kind": "sheet_rows", "sheet_id": source.sheet_id},
            sheet_name="Findings",
            output_names={"match": "Match"},
            idempotency_key="find-source-test",
            params={
                "source": "transcript",
                "instruction": "Find both occurrences.",
                "model": "anthropic/claude-haiku-4-5",
            },
        )
    )
    late = {
        "match": "LATE",
        "source_unit_ids": ["segment-23", "segment-25"],
        "details": {},
    }
    adapter = _FindAdapter(
        "segment-0",
        matches_by_call=[
            [late],
            [
                {
                    "match": "MIDDLE",
                    "source_unit_ids": ["segment-24"],
                    "details": {},
                },
                late,
            ],
        ],
    )
    router = ModelRouter(keys={"anthropic": "test"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter  # noqa: SLF001

    scan = asyncio.run(
        scan_find_plans(params, plan_find_scan(params, (source,)), router=router)
    )

    assert [match.candidate.match for match in scan.matches] == ["MIDDLE", "LATE"]
    assert len(adapter.requests) == 2
    assert scan.issues == ()


def test_scan_preserves_visual_region_page_order() -> None:
    output = io.BytesIO()
    Image.new("RGB", (100, 60), color="white").save(output, format="PNG")
    source = ResolvedFindSource(
        sheet_id=1,
        row_id=2,
        column_id=3,
        column_type="image",
        source_snapshot="snapshot",
        value_ref={},
        artifact_id=None,
        units=(),
        source_kind="image",
        image=prepare_image_asset(output.getvalue(), source_media_type="image/png"),
        blob_hash="sha256:test",
        filename="regions.png",
        media_type="image/png",
    )
    params = _operation_for_request(
        ActionRequest(
            action_id="map.find",
            scope={"kind": "sheet_rows", "sheet_id": source.sheet_id},
            sheet_name="Findings",
            output_names={"match": "Match"},
            idempotency_key="find-source-test",
            params={
                "source": "image",
                "instruction": "Find both rectangles.",
                "model": "anthropic/claude-sonnet-5",
            },
        )
    )
    adapter = _VisualFindAdapter(
        matches=[
            {
                "match": "bottom",
                "bbox_pixels_xyxy": [10, 40, 30, 50],
                "details": {},
            },
            {
                "match": "top",
                "bbox_pixels_xyxy": [10, 5, 30, 15],
                "details": {},
            },
        ]
    )
    router = ModelRouter(keys={"anthropic": "test"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter  # noqa: SLF001

    scan = asyncio.run(
        scan_find_plans(params, plan_find_scan(params, (source,)), router=router)
    )

    assert all(isinstance(match, VisualFindMatch) for match in scan.matches)
    assert [match.region.match for match in scan.matches] == ["top", "bottom"]
    assert scan.issues == ()


def test_find_action_job_writes_one_grounded_row_per_match(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "find-runtime.frisket", name="Find runtime")
    try:
        sheet_id = project.add_sheet("Interviews")
        source_column_id = project.add_column(sheet_id, "transcript", "text")
        source_row_id = project.add_rows(
            sheet_id,
            [
                {
                    "transcript": (
                        "Opening remarks. China trade policy changed this year. "
                        "Closing remarks."
                    )
                }
            ],
            {"transcript": source_column_id},
        )[0]
        action = ActionRequest.model_validate(
            {
                "action_id": "map.find",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": [source_row_id],
                },
                "sheet_name": "Findings",
                "output_names": {"match": "Match"},
                "params": {
                    "source": "transcript",
                    "instruction": "Find every discussion of China.",
                    "fields": [
                        {
                            "name": "sentiment",
                            "type": "category",
                            "labels": ["positive", "neutral", "negative"],
                        }
                    ],
                    "model": "anthropic/claude-haiku-4-5",
                },
                "idempotency_key": "research-find-runtime@sha256:test",
            }
        )
        params = _operation_for_request(action)
        sources = resolve_find_sources(project, params)
        assert isinstance(sources, tuple)
        plans = plan_find_scan(params, sources)
        envelope = _queued_find(project, action, project_id="project-find-runtime")
        reserved = {"receipt_id": envelope.receipt_id, "action_id": envelope.action_id}
        admitted = dict(envelope.resolved_snapshot)

        adapter = _FindAdapter(str(sources[0].units[0].unit_id))
        router = ModelRouter(keys={"anthropic": "test"}, cache=None, cache_mode="off")
        router._adapters["anthropic"] = adapter  # noqa: SLF001
        bindings = action_job_bindings(
            executor_deps_factory=lambda _project_id, _run_id: ExecutorDeps(
                router=router
            )
        )

        result = run_action_run_job(
            project,
            {"action_job": envelope.to_json(), "job_id": 7},
            executor_lookup=bindings.get,
        )

        assert result.status == "completed"
        assert len(adapter.requests) == len(plans) == 1
        findings = project.db.execute(
            "SELECT id FROM sheets WHERE name='Findings' AND hidden=0"
        ).fetchone()
        assert findings is not None
        findings_sheet_id = int(findings["id"])
        columns = {
            column["name"]: int(column["id"])
            for column in project.columns(findings_sheet_id)
        }
        assert set(columns) == {"Match", "sentiment"}
        finding_row_id = project.visible_row_ids(findings_sheet_id)[0]
        assert (
            project.get_values(
                findings_sheet_id, columns["Match"], row_ids=[finding_row_id]
            )[finding_row_id]
            == "China trade policy"
        )
        assert (
            project.get_values(
                findings_sheet_id, columns["sentiment"], row_ids=[finding_row_id]
            )[finding_row_id]
            == "neutral"
        )
        evidence = list_row_evidence(
            project, sheet_id=findings_sheet_id, row_id=finding_row_id
        )
        assert len(evidence["links"]) == 1
        viewer = resolve_evidence_viewer(project, evidence["links"][0]["stable_id"])
        span = viewer["artifacts"][0]["spans"][0]
        assert span["span_kind"] == "text"
        assert "China trade policy" in span["quote"]
        receipt = ReceiptStore(project).parsed_by_id(reserved["receipt_id"])
        assert receipt is not None
        assert receipt.status == "completed"
        coverage = next(
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "map_find_coverage"
        )
        assert coverage == {
            "kind": "map_find_coverage",
            "status": "complete",
            "window_count": 1,
            "match_count": 1,
            "resolved_prompt_hash": admitted["resolved_prompt_hash"],
        }
    finally:
        project.close()


def test_find_source_change_records_definite_pre_egress_outcome(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "find-source-change.frisket", name="Find")
    try:
        sheet_id = project.add_sheet("Sources")
        column_id = project.add_column(sheet_id, "body", "text")
        row_id = project.add_rows(
            sheet_id,
            [{"body": "Original source"}],
            {"body": column_id},
        )[0]
        action = ActionRequest.model_validate(
            {
                "action_id": "map.find",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": [row_id],
                },
                "sheet_name": "Findings",
                "output_names": {"match": "Match"},
                "params": {
                    "source": "body",
                    "instruction": "Find every mention of Frisket.",
                    "model": "anthropic/claude-haiku-4-5",
                },
                "idempotency_key": "map-find-source-change@sha256:test",
            }
        )
        params = _operation_for_request(action)
        sources = resolve_find_sources(project, params)
        assert isinstance(sources, tuple)
        envelope = _queued_find(
            project, action, project_id="project-find-source-change"
        )
        reserved = {"receipt_id": envelope.receipt_id, "action_id": envelope.action_id}

        replace_test_source_cell(
            project,
            row_id=row_id,
            column_id=column_id,
            value="Changed before execution",
        )

        result = run_action_run_job(
            project,
            {"action_job": envelope.to_json(), "job_id": 9},
            executor_lookup=action_job_bindings().get,
        )

        assert result.status == "failed"
        assert result.errors[0].code == "source_changed"
        receipt = ReceiptStore(project).parsed_by_id(reserved["receipt_id"])
        assert receipt is not None
        assert receipt.provider_use == []
        assert [
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "map_find_effect_outcome"
        ] == [
            {
                "kind": "map_find_effect_outcome",
                "schema_version": "frisket.map_find_effect_outcome.v1",
                "external_effect": "none",
                "reason": "source_changed",
            }
        ]
        assert project.db.execute("SELECT 1 FROM model_calls").fetchone() is None
    finally:
        project.close()


def test_find_action_job_locates_raw_image_regions_with_same_call_details(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "find-image.frisket", name="Find image")
    try:
        sheet_id = project.add_sheet("Images")
        source_column_id = project.add_column(sheet_id, "image", "image")
        output = io.BytesIO()
        Image.new("RGB", (100, 60), color="white").save(output, format="PNG")
        blob_hash = project.add_blob(
            output.getvalue(), filename="regions.png", mime="image/png"
        )
        source_row_id = project.add_rows(
            sheet_id,
            [
                {
                    "image": {
                        "blob": blob_hash,
                        "filename": "regions.png",
                        "mime": "image/png",
                    }
                }
            ],
            {"image": source_column_id},
        )[0]
        action = ActionRequest.model_validate(
            {
                "action_id": "map.find",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": [source_row_id],
                },
                "sheet_name": "Findings",
                "output_names": {"match": "Match"},
                "params": {
                    "source": "image",
                    "instruction": "Find every blue rectangle.",
                    "fields": [
                        {
                            "name": "sentiment",
                            "type": "category",
                            "labels": ["positive", "neutral", "negative"],
                        }
                    ],
                    "model": "anthropic/claude-sonnet-5",
                },
                "idempotency_key": "map-find-image-runtime@sha256:test",
            }
        )
        params = _operation_for_request(action)
        sources = resolve_find_sources(project, params)
        assert isinstance(sources, tuple)
        assert sources[0].source_kind == "image"
        plans = plan_find_scan(params, sources)
        estimate = estimate_find_scan_cost(params, plans)
        assert estimate["estimated_image_input_tokens_per_plan"] == 2_048
        assert estimate["input_tokens"] >= 2_048
        envelope = _queued_find(
            project, action, project_id="project-find-image-runtime"
        )

        adapter = _VisualFindAdapter()
        router = ModelRouter(keys={"anthropic": "test"}, cache=None, cache_mode="off")
        router._adapters["anthropic"] = adapter  # noqa: SLF001
        bindings = action_job_bindings(
            executor_deps_factory=lambda _project_id, _run_id: ExecutorDeps(
                router=router
            )
        )

        result = run_action_run_job(
            project,
            {"action_job": envelope.to_json(), "job_id": 8},
            executor_lookup=bindings.get,
        )

        assert result.status == "completed"
        assert len(adapter.requests) == 1
        content = adapter.requests[0].messages[0]["content"]
        assert any(part.get("type") == "image" for part in content)
        findings = project.db.execute(
            "SELECT id FROM sheets WHERE name='Findings' AND hidden=0"
        ).fetchone()
        assert findings is not None
        findings_sheet_id = int(findings["id"])
        columns = {
            column["name"]: int(column["id"])
            for column in project.columns(findings_sheet_id)
        }
        finding_row_id = project.visible_row_ids(findings_sheet_id)[0]
        assert (
            project.get_values(
                findings_sheet_id, columns["Match"], row_ids=[finding_row_id]
            )[finding_row_id]
            == "blue rectangle"
        )
        assert (
            project.get_values(
                findings_sheet_id, columns["sentiment"], row_ids=[finding_row_id]
            )[finding_row_id]
            == "neutral"
        )
        evidence = list_row_evidence(
            project, sheet_id=findings_sheet_id, row_id=finding_row_id
        )
        viewer = resolve_evidence_viewer(project, evidence["links"][0]["stable_id"])
        artifact = viewer["artifacts"][0]
        span = artifact["spans"][0]
        assert artifact["media_type"] == "image/png"
        assert artifact["metadata"]["page_images"]["1"] == {
            "blob_hash": blob_hash,
            "height": 60,
            "width": 100,
        }
        assert span["span_kind"] == "region"
        assert span["selector"]["bbox"][0] == {
            "space": "page_normalized",
            "x0": 0.1,
            "y0": 0.2,
            "x1": 0.5,
            "y1": 0.7,
            "raw": {
                "space": "pixel",
                "x0": 10.0,
                "y0": 12.0,
                "x1": 50.0,
                "y1": 42.0,
            },
        }
        assert span["raw"]["grounding_method"] == "vision_model_region"
    finally:
        project.close()


def test_timestamped_find_row_exposes_original_video_start_end_and_clip(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "find-video.frisket", name="Find video")
    try:
        sheet_id, source_row_id = _seed_timestamped_video(project)
        action = ActionRequest.model_validate(
            {
                "action_id": "map.find",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": [source_row_id],
                },
                "sheet_name": "Video findings",
                "output_names": {"match": "Match"},
                "params": {
                    "source": "transcript",
                    "instruction": "Find every discussion of China.",
                    "fields": [],
                    "model": "anthropic/claude-haiku-4-5",
                },
                "idempotency_key": "research-find-video@sha256:test",
            }
        )
        params = _operation_for_request(action)
        sources = resolve_find_sources(project, params)
        assert isinstance(sources, tuple)
        source = sources[0]
        unit = source.units[1]
        envelope = _queued_find(project, action, project_id="project-find-video")
        adapter = _FindAdapter(
            str(unit.unit_id),
            matches=[
                {
                    "match": "China trade policy",
                    "source_unit_ids": [unit.unit_id],
                    "details": {},
                }
            ],
        )
        result = _run_queued_find(project, envelope, adapter)

        assert result.status == "completed"
        assert len(adapter.requests) == 1
        findings_sheet_id = next(
            output.sheet_id for output in result.outputs if output.kind == "sheet"
        )
        assert findings_sheet_id is not None
        finding_row_id = project.visible_row_ids(findings_sheet_id)[0]
        evidence = list_row_evidence(
            project,
            sheet_id=findings_sheet_id,
            row_id=finding_row_id,
            project_id="project-find-video",
        )
        viewer = resolve_evidence_viewer(
            project,
            evidence["links"][0]["stable_id"],
            project_id="project-find-video",
        )
        artifact = viewer["artifacts"][0]
        assert artifact["media_type"] == "video/mp4"
        assert [
            (span["selector"]["start_ms"], span["selector"]["end_ms"])
            for span in artifact["spans"]
        ] == [(2_000, 7_000)]
        assert artifact["runs"] == [
            {
                "index": 0,
                "start_ms": 2_000,
                "end_ms": 7_000,
                "span_ids": [artifact["spans"][0]["stable_id"]],
                "clip_url": (
                    "/api/projects/project-find-video/evidence/links/"
                    f"{evidence['links'][0]['stable_id']}/runs/0/clip"
                ),
            }
        ]
    finally:
        project.close()


def test_uncited_sibling_is_withheld_and_result_is_partial(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "find-partial.frisket", name="Find partial")
    try:
        sheet_id = project.add_sheet("Sources")
        source_column_id = project.add_column(sheet_id, "body", "text")
        source_row_id = project.add_rows(
            sheet_id,
            [{"body": "China trade policy changed this year."}],
            {"body": source_column_id},
        )[0]
        action = ActionRequest.model_validate(
            {
                "action_id": "map.find",
                "scope": {
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": [source_row_id],
                },
                "sheet_name": "Findings",
                "output_names": {"match": "Match"},
                "params": {
                    "source": "body",
                    "instruction": "Find every discussion of China.",
                    "fields": [
                        {
                            "name": "sentiment",
                            "type": "category",
                            "labels": ["positive", "neutral", "negative"],
                        }
                    ],
                    "model": "anthropic/claude-haiku-4-5",
                },
                "idempotency_key": "research-find-partial@sha256:test",
            }
        )
        params = _operation_for_request(action)
        sources = resolve_find_sources(project, params)
        assert isinstance(sources, tuple)
        envelope = _queued_find(project, action, project_id="project-find-partial")
        reserved = {"receipt_id": envelope.receipt_id, "action_id": envelope.action_id}

        adapter = _FindAdapter(str(sources[0].units[0].unit_id), include_uncited=True)
        router = ModelRouter(keys={"anthropic": "test"}, cache=None, cache_mode="off")
        router._adapters["anthropic"] = adapter  # noqa: SLF001
        bindings = action_job_bindings(
            executor_deps_factory=lambda _project_id, _run_id: ExecutorDeps(
                router=router
            )
        )

        result = run_action_run_job(
            project,
            {"action_job": envelope.to_json(), "job_id": 8},
            executor_lookup=bindings.get,
        )

        assert result.status == "partial"
        findings = project.db.execute(
            "SELECT id FROM sheets WHERE name='Findings' AND hidden=0"
        ).fetchone()
        assert findings is not None
        assert len(project.visible_row_ids(int(findings["id"]))) == 1, [
            error.model_dump() for error in result.errors
        ]
        receipt = ReceiptStore(project).parsed_by_id(reserved["receipt_id"])
        assert receipt is not None
        assert receipt.status == "partial"
        coverage = next(
            item.ref
            for item in receipt.evidence
            if item.ref.get("kind") == "map_find_coverage"
        )
        assert coverage["status"] == "incomplete"
        assert receipt.errors[0].code == "citation_validation_failed"

        failed_action = action.model_copy(
            update={
                "sheet_name": "Failed findings",
                "idempotency_key": "map-find-total-failure@sha256:test",
            }
        )
        failed_envelope = _queued_find(
            project, failed_action, project_id="project-find-total-failure"
        )
        failed_reserved = {
            "receipt_id": failed_envelope.receipt_id,
            "action_id": failed_envelope.action_id,
        }

        invalid_adapter = _FindAdapter(
            str(sources[0].units[0].unit_id),
            matches=[
                {
                    "match": "Fabricated excerpt",
                    "source_unit_ids": [sources[0].units[0].unit_id],
                    "details": {},
                }
            ],
        )
        failed_router = ModelRouter(
            keys={"anthropic": "test"}, cache=None, cache_mode="off"
        )
        failed_router._adapters["anthropic"] = invalid_adapter  # noqa: SLF001
        failed_bindings = action_job_bindings(
            executor_deps_factory=lambda _project_id, _run_id: ExecutorDeps(
                router=failed_router
            )
        )

        failed = run_action_run_job(
            project,
            {"action_job": failed_envelope.to_json(), "job_id": 9},
            executor_lookup=failed_bindings.get,
        )

        assert failed.status == "failed"
        assert failed.errors[0].code == "incomplete_scan"
        failed_receipt = ReceiptStore(project).parsed_by_id(
            failed_reserved["receipt_id"]
        )
        assert failed_receipt is not None
        assert not any(
            item.ref.get("kind") == "map_find_effect_outcome"
            for item in failed_receipt.evidence
        )
        assert (
            project.db.execute(
                "SELECT id FROM sheets WHERE name='Failed findings' AND hidden=0"
            ).fetchone()
            is None
        )
    finally:
        project.close()


def test_selected_video_resolves_its_compatible_timestamped_transcript(
    tmp_path: Path,
) -> None:
    project = Project.create(
        tmp_path / "find-selected-video.frisket", name="Find video"
    )
    try:
        sheet_id, source_row_id = _seed_timestamped_video(project)
        params = _operation_for_request(
            ActionRequest(
                action_id="map.find",
                scope={
                    "kind": "sheet_rows",
                    "sheet_id": sheet_id,
                    "row_ids": [source_row_id],
                },
                sheet_name="Findings",
                output_names={"match": "Match"},
                idempotency_key="find-source-test",
                params={
                    "source": "video",
                    "instruction": "Find every discussion of China.",
                    "model": "anthropic/claude-haiku-4-5",
                },
            )
        )

        sources = resolve_find_sources(project, params)

        assert isinstance(sources, tuple)
        assert len(sources) == 1
        assert [unit.text for unit in sources[0].units] == [
            "Opening.",
            "China trade policy.",
            "Closing.",
        ]
        assert sources[0].artifact_id is not None
    finally:
        project.close()


def test_selected_video_without_transcript_returns_scoped_remediation(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "find-raw-video.frisket", name="Raw video")
    try:
        sheet_id = project.add_sheet("Video")
        video_column_id = project.add_column(sheet_id, "video", "video")
        blob_hash = project.add_blob(
            b"video bytes",
            filename="source.mp4",
            mime="video/mp4",
            metadata=owned_media_metadata_document(probe={"duration_seconds": 12.0}),
        )
        row_id = project.add_rows(
            sheet_id,
            [
                {
                    "video": {
                        "blob": blob_hash,
                        "filename": "source.mp4",
                        "mime": "video/mp4",
                    }
                }
            ],
            {"video": video_column_id},
        )[0]
        params = _operation_for_request(
            ActionRequest(
                action_id="map.find",
                scope={"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": [row_id]},
                sheet_name="Findings",
                output_names={"match": "Match"},
                idempotency_key="find-source-test",
                params={
                    "source": "video",
                    "instruction": "Find every discussion of China.",
                    "model": "anthropic/claude-haiku-4-5",
                },
            )
        )

        error = resolve_find_sources(project, params)

        assert error.code == "source_needs_transcript"
        assert error.details == {
            "sheet_id": sheet_id,
            "row_ids": [row_id],
            "source_column": "video",
            "remediation_action_kind": "media.transcribe",
        }
    finally:
        project.close()


def test_selected_image_uses_vision_while_ocr_text_reuses_region_evidence(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "find-ocr.frisket", name="Find OCR")
    try:
        sheet_id = project.add_sheet("Scans")
        file_column_id = project.add_column(sheet_id, "scan", "file")
        text_column_id = project.add_column(
            sheet_id, "ocr_text", "text", ai_generated=True
        )
        output = io.BytesIO()
        Image.new("RGB", (100, 60), color="white").save(output, format="PNG")
        blob_hash = project.add_blob(
            output.getvalue(), filename="scan.png", mime="image/png"
        )
        row_id = project.add_rows(
            sheet_id,
            [
                {
                    "scan": {
                        "blob": blob_hash,
                        "filename": "scan.png",
                        "mime": "image/png",
                    },
                    "ocr_text": "China trade policy",
                }
            ],
            {"scan": file_column_id, "ocr_text": text_column_id},
        )[0]
        artifact = record_source_artifact(
            project,
            artifact_kind="file",
            media_type="image/png",
            blob_hash=blob_hash,
            filename="scan.png",
            source_sheet_id=sheet_id,
            source_row_id=row_id,
            source_column_id=file_column_id,
            metadata={"engine": "fixture", "page_images": {}},
        )
        region = record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="region",
            page_start=1,
            page_end=1,
            quote="China trade policy",
            bbox=[{"x0": 0.1, "y0": 0.2, "x1": 0.8, "y1": 0.3}],
        )
        _values, refs = project.get_values_with_refs(
            sheet_id, text_column_id, row_ids=[row_id]
        )
        record_evidence_link(
            project,
            subject_kind="cell",
            subject_ref=refs[row_id],
            spans=[{"span_id": region["id"]}],
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=text_column_id,
            link_role="media_ocr_grounding",
            producer={"action_kind": "media.ocr"},
        )

        def resolve(column: str) -> Any:
            return resolve_find_sources(
                project,
                _operation_for_request(
                    ActionRequest(
                        action_id="map.find",
                        scope={
                            "kind": "sheet_rows",
                            "sheet_id": sheet_id,
                            "row_ids": [row_id],
                        },
                        sheet_name="Findings",
                        output_names={"match": "Match"},
                        idempotency_key="find-source-test",
                        params={
                            "source": column,
                            "instruction": "Find China.",
                            "model": "anthropic/claude-haiku-4-5",
                        },
                    )
                ),
            )

        selected_file = resolve("scan")
        selected_text = resolve("ocr_text")

        assert isinstance(selected_file, tuple)
        assert isinstance(selected_text, tuple)
        assert selected_file[0].source_kind == "image"
        assert selected_file[0].image is not None
        assert selected_file[0].units == ()
        assert selected_text[0].source_kind == "text"
        assert selected_text[0].artifact_id == artifact["id"]
        assert selected_text[0].units[0].span_ids == (region["id"],)
    finally:
        project.close()


def test_selected_file_reuses_legacy_summary_only_ocr_evidence(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "find-legacy-ocr.frisket", name="Legacy OCR")
    try:
        sheet_id = project.add_sheet("Documents")
        file_column_id = project.add_column(sheet_id, "document", "file")
        text_column_id = project.add_column(
            sheet_id, "ocr_text", "text", ai_generated=True
        )
        blob_hash = project.add_blob(
            b"%PDF-1.4\nlegacy fixture",
            filename="legacy.pdf",
            mime="application/pdf",
        )
        row_id = project.add_rows(
            sheet_id,
            [
                {
                    "document": {
                        "blob": blob_hash,
                        "filename": "legacy.pdf",
                        "mime": "application/pdf",
                    },
                    "ocr_text": "China trade policy",
                }
            ],
            {"document": file_column_id, "ocr_text": text_column_id},
        )[0]
        artifact = record_source_artifact(
            project,
            artifact_kind="file",
            media_type="application/pdf",
            blob_hash=blob_hash,
            filename="legacy.pdf",
            source_sheet_id=sheet_id,
            source_row_id=row_id,
            source_column_id=file_column_id,
            metadata={"engine": "legacy-ocr", "page_images": {"1": {}}},
        )
        summary = record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="page_range",
            page_start=1,
            page_end=1,
            snippet="China trade policy",
        )
        _values, refs = project.get_values_with_refs(
            sheet_id, text_column_id, row_ids=[row_id]
        )
        record_evidence_link(
            project,
            subject_kind="cell",
            subject_ref=refs[row_id],
            spans=[{"span_id": summary["id"]}],
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=text_column_id,
            link_role="media_ocr_grounding",
            producer={"action_kind": "media.ocr"},
        )

        resolved = resolve_find_sources(
            project,
            _operation_for_request(
                ActionRequest(
                    action_id="map.find",
                    scope={
                        "kind": "sheet_rows",
                        "sheet_id": sheet_id,
                        "row_ids": [row_id],
                    },
                    sheet_name="Findings",
                    output_names={"match": "Match"},
                    idempotency_key="find-source-test",
                    params={
                        "source": "document",
                        "instruction": "Find China.",
                        "model": "anthropic/claude-haiku-4-5",
                    },
                )
            ),
        )

        assert isinstance(resolved, tuple)
        assert [unit.text for unit in resolved[0].units] == ["China trade policy"]
        assert resolved[0].units[0].span_ids == (summary["id"],)
    finally:
        project.close()


def test_find_rerun_atomically_supersedes_managed_generation(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "find-replace.frisket", name="Find replace")
    try:
        sheet_id = project.add_sheet("Sources")
        body_column_id = project.add_column(sheet_id, "body", "text")
        source_row_id = project.add_rows(
            sheet_id,
            [{"body": "China trade policy changed this year."}],
            {"body": body_column_id},
        )[0]

        def publish(
            *,
            key: str,
            match_text: str | None,
            fields: list[dict[str, Any]],
            details: dict[str, Any],
        ) -> tuple[Any, str]:
            action = ActionRequest.model_validate(
                {
                    "action_id": "map.find",
                    "scope": {
                        "kind": "sheet_rows",
                        "sheet_id": sheet_id,
                        "row_ids": [source_row_id],
                    },
                    "sheet_name": "Findings",
                    "output_names": {"match": "Match"},
                    "params": {
                        "source": "body",
                        "instruction": "Find every discussion of China.",
                        "fields": fields,
                        "model": "anthropic/claude-haiku-4-5",
                    },
                    "idempotency_key": key,
                }
            )
            params = _operation_for_request(action)
            sources = resolve_find_sources(project, params)
            assert isinstance(sources, tuple)
            source = sources[0]
            unit = source.units[0]
            matches = []
            if match_text is not None:
                matches.append(
                    {
                        "match": match_text,
                        "source_unit_ids": [unit.unit_id],
                        "details": details,
                    }
                )
            envelope = _queued_find(project, action, project_id="project-find-replace")
            adapter = _FindAdapter(str(unit.unit_id), matches=matches)
            result = _run_queued_find(project, envelope, adapter)
            assert len(adapter.requests) == 1
            return result, envelope.receipt_id

        first, _first_receipt_id = publish(
            key="research-find-replace@sha256:first",
            match_text="China trade policy",
            fields=[{"name": "sentiment", "type": "text"}],
            details={"sentiment": "neutral"},
        )
        assert first.status == "completed"
        findings_sheet_id = next(
            output.sheet_id for output in first.outputs if output.kind == "sheet"
        )
        assert findings_sheet_id is not None
        first_row_id = project.visible_row_ids(findings_sheet_id)[0]
        first_evidence = list_row_evidence(
            project, sheet_id=findings_sheet_id, row_id=first_row_id
        )["links"][0]

        second, _second_receipt_id = publish(
            key="research-find-replace@sha256:second",
            match_text="trade policy changed",
            fields=[{"name": "speaker", "type": "text"}],
            details={"speaker": "S1"},
        )

        assert second.status == "completed"
        replacement_sheet_id = next(
            output.sheet_id for output in second.outputs if output.kind == "sheet"
        )
        assert replacement_sheet_id is not None
        assert replacement_sheet_id != findings_sheet_id
        visible_columns = {
            str(column["name"]): int(column["id"])
            for column in project.columns(replacement_sheet_id)
        }
        assert set(visible_columns) == {"Match", "speaker"}
        second_row_ids = project.visible_row_ids(replacement_sheet_id)
        assert len(second_row_ids) == 1
        assert second_row_ids[0] != first_row_id
        assert (
            project.get_values(
                replacement_sheet_id,
                visible_columns["Match"],
                row_ids=second_row_ids,
            )[second_row_ids[0]]
            == "trade policy changed"
        )
        old_sheet = project.db.execute(
            "SELECT hidden, name FROM sheets WHERE id=?", (findings_sheet_id,)
        ).fetchone()
        assert int(old_sheet["hidden"]) == 1
        assert str(old_sheet["name"]).startswith("Findings (superseded:")
        assert (
            project.db.execute(
                "SELECT 1 FROM rows WHERE id=?", (first_row_id,)
            ).fetchone()
            is not None
        )
        stale = project.db.execute(
            "SELECT status, stale_reason FROM evidence_links WHERE stable_id=?",
            (first_evidence["stable_id"],),
        ).fetchone()
        assert dict(stale) == {
            "status": "stale",
            "stale_reason": "superseded_find_generation",
        }
        current_op = project.db.execute(
            "SELECT parent_op_id FROM sheets WHERE id=?", (replacement_sheet_id,)
        ).fetchone()
        assert int(current_op["parent_op_id"]) == second.op_ids[0]

        third, _third_receipt_id = publish(
            key="research-find-replace@sha256:third",
            match_text=None,
            fields=[],
            details={},
        )
        assert third.status == "completed"
        empty_sheet_id = next(
            output.sheet_id for output in third.outputs if output.kind == "sheet"
        )
        assert empty_sheet_id is not None
        assert project.visible_row_ids(empty_sheet_id) == []
    finally:
        project.close()
