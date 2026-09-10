"""HTTP-06-F4: project-source read routes have exact typed HTTP owners."""

from __future__ import annotations

import importlib
import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, get_args, get_origin

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel, JsonValue, RootModel, ValidationError

from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG
from frisket.contracts.http.models import HttpError, WireModel
from frisket.engine.store.sources import SourceStore
from frisket.server.app import create_app


_CONTRACT_MODULE = "frisket.contracts.http.project_sources"
_RAW_SOURCE_FIELDS = frozenset(
    {
        "id",
        "name",
        "kind",
        "url",
        "config",
        "sheet_id",
        "schedule",
        "enabled",
        "cursor",
        "last_checked_at",
        "last_status",
        "new_rows_total",
        "created_at",
    }
)
# Exact current source_runs rows. The initial F4 handoff said 21 fields; the
# frozen base schema and service return 18, and this RED intentionally follows
# the executable source of truth rather than widening a nonexistent shape.
_RAW_RUN_FIELDS = frozenset(
    {
        "id",
        "source_id",
        "op_id",
        "receipt_id",
        "status",
        "new_rows",
        "skipped_rows",
        "changed_rows",
        "revisions",
        "error",
        "cursor_before",
        "cursor_after",
        "duration_ms",
        "warning_count",
        "cost_micro",
        "summary_json",
        "started_at",
        "finished_at",
    }
)
_PAGE_FIELDS = frozenset(
    {
        "schema_version",
        "order",
        "offset",
        "limit",
        "total",
        "has_more",
        "next_offset",
        "latest_run",
        "latest_run_loaded",
    }
)
_HEALTH_SOURCE_FIELDS = frozenset(
    {
        "id",
        "name",
        "kind",
        "url",
        "enabled",
        "schedule",
        "sheet_id",
        "created_at",
        "redactions",
    }
)
_HEALTH_SUMMARY_FIELDS = frozenset(
    {
        "status",
        "last_success_at",
        "last_failure_at",
        "consecutive_failures",
        "new_rows_total",
        "new_rows_recent",
        "changed_rows_recent",
        "skipped_rows_recent",
        "revisions_recent",
        "recent_run_count",
        "last_cursor_summary",
    }
)
_HEALTH_PAGE_FIELDS = frozenset(
    {
        "schema_version",
        "order",
        "offset",
        "limit",
        "total",
        "has_more",
        "next_offset",
    }
)
_HEALTH_RUN_FIELDS = frozenset(
    {
        "id",
        "source_id",
        "status",
        "started_at",
        "finished_at",
        "receipt_id",
        "op_id",
        "new_rows",
        "skipped_rows",
        "changed_rows",
        "revisions",
        "duration_ms",
        "warning_count",
        "cost_micro",
        "error_summary",
        "cursor_before_present",
        "cursor_after_present",
        "summary_present",
    }
)
_HEALTH_JOB_FIELDS = frozenset(
    {
        "job_id",
        "kind",
        "status",
        "attempts",
        "max_attempts",
        "created_at",
        "started_at",
        "finished_at",
        "refs",
        "result_summary",
        "error_summary",
        "stalled",
    }
)
_HEALTH_SPARSE_NOTICE_FIELDS = frozenset({"code", "message", "run_ids"})
_HEALTH_UNSUPPORTED_SCHEDULE_NOTICE_FIELDS = frozenset({"code", "message"})
_HEALTH_REFS_FIELDS = frozenset({"source_id", "source_run_id", "sheet_id"})

_SOURCE_NULLABLE_FIELDS = frozenset(
    {
        "url",
        "sheet_id",
        "schedule",
        "cursor",
        "last_checked_at",
        "last_status",
    }
)
_RUN_NULLABLE_FIELDS = frozenset(
    {
        "op_id",
        "receipt_id",
        "error",
        "cursor_before",
        "cursor_after",
        "duration_ms",
        "finished_at",
    }
)
_HEALTH_SOURCE_NULLABLE_FIELDS = frozenset({"url", "schedule", "sheet_id"})
_HEALTH_SUMMARY_NULLABLE_FIELDS = frozenset({"last_success_at", "last_failure_at"})
_HEALTH_PAGE_NULLABLE_FIELDS = frozenset({"next_offset"})
_HEALTH_RUN_NULLABLE_FIELDS = frozenset(
    {"finished_at", "receipt_id", "op_id", "duration_ms", "error_summary"}
)
_HEALTH_JOB_NULLABLE_FIELDS = frozenset({"started_at", "finished_at", "error_summary"})


@dataclass(frozen=True)
class RouteTruth:
    path: str
    response: str
    errors: frozenset[int]
    default_limit: int | None = None


ROUTES = {
    "list_sources": RouteTruth(
        "/api/projects/{pid}/sources",
        "ProjectSourceList",
        frozenset({401, 403, 404, 409, 500}),
    ),
    "get_source_ep": RouteTruth(
        "/api/projects/{pid}/sources/{source_id}",
        "ProjectSourceDetail",
        frozenset({401, 403, 404, 409, 422, 500}),
        50,
    ),
    "get_source_health_ep": RouteTruth(
        "/api/projects/{pid}/sources/{source_id}/health",
        "ProjectSourceHealth",
        frozenset({401, 403, 404, 409, 422, 500}),
        20,
    ),
}
OPERATION_IDS = frozenset(
    {
        "tenant.list_sources.get",
        "tenant.get_source_ep.get",
        "tenant.get_source_health_ep.get",
    }
)


def _contracts() -> ModuleType:
    spec = importlib.util.find_spec(_CONTRACT_MODULE)
    if spec is None:
        pytest.fail(
            "INTENDED_F4_RED: project_sources typed contract module must exist",
            pytrace=False,
        )
    return importlib.import_module(_CONTRACT_MODULE)


def _model(module: ModuleType, name: str) -> type[BaseModel]:
    candidate = getattr(module, name, None)
    if not isinstance(candidate, type) or not issubclass(candidate, BaseModel):
        pytest.fail(
            f"INTENDED_F4_RED: project_sources.{name} must be a Pydantic model",
            pytrace=False,
        )
    return candidate


def _assert_closed_annotation(annotation: object, *, label: str) -> None:
    """Reject an unbounded DTO field without banning ``JsonValue`` leaves."""

    if annotation is Any:
        pytest.fail(f"INTENDED_F4_RED: {label} must not use Any", pytrace=False)
    origin = get_origin(annotation)
    if (
        annotation in (dict, list, object)
        or origin in (dict, list)
        and not get_args(annotation)
    ):
        pytest.fail(
            f"INTENDED_F4_RED: {label} must not use an unparameterized object",
            pytrace=False,
        )
    for child in get_args(annotation):
        _assert_closed_annotation(child, label=label)


def _assert_closed_wire_model(model: type[BaseModel]) -> None:
    assert model.model_config.get("extra") == "forbid"
    assert model.model_config.get("strict") is True
    for name, field in model.model_fields.items():
        _assert_closed_annotation(field.annotation, label=f"{model.__name__}.{name}")


def _assert_all_fields_required(model: type[BaseModel]) -> None:
    assert all(field.is_required() for field in model.model_fields.values())


def _assert_required_nullable(
    model: type[BaseModel], field_names: frozenset[str]
) -> None:
    for name in field_names:
        field = model.model_fields[name]
        assert field.is_required(), f"{model.__name__}.{name} must remain required"
        assert type(None) in get_args(field.annotation), (
            f"{model.__name__}.{name} must remain nullable"
        )


def _assert_optional_nullable(model: type[BaseModel], name: str) -> None:
    field = model.model_fields[name]
    assert not field.is_required(), f"{model.__name__}.{name} must stay omittable"
    assert type(None) in get_args(field.annotation), (
        f"{model.__name__}.{name} must stay nullable when present"
    )


def _assert_missing_fields_rejected(
    model: type[BaseModel], payload: dict[str, object], field_names: frozenset[str]
) -> None:
    for name in field_names:
        with pytest.raises(ValidationError):
            model.model_validate(
                {key: value for key, value in payload.items() if key != name}
            )


def _query_metadata_bounds(field: Any) -> dict[str, int]:
    """Read FastAPI's canonical annotated-query constraints without legacy attrs."""

    bounds: list[tuple[str, int]] = []
    for metadata in field.field_info.metadata:
        for name in ("ge", "le"):
            value = getattr(metadata, name, None)
            if value is not None:
                assert type(value) is int
                bounds.append((name, value))
    assert bounds, "query constraints must be represented in field_info.metadata"
    assert len(bounds) == len({name for name, _ in bounds})
    return dict(bounds)


@pytest.fixture(scope="module")
def routes_by_name(tmp_path_factory: pytest.TempPathFactory) -> dict[str, APIRoute]:
    app = create_app(tmp_path_factory.mktemp("project-source-http-contracts"))
    return {
        route.name: route
        for route in app.routes
        if isinstance(route, APIRoute) and route.name in ROUTES
    }


def test_exact_three_source_reads_have_policy_identity_auth_and_browser_membership() -> (
    None
):
    entries = [entry for entry in BASE_ENDPOINT_CATALOG if entry.id in OPERATION_IDS]
    assert {
        (
            entry.id,
            entry.route_owner,
            entry.route_name,
            entry.method,
            entry.auth,
            entry.project_role,
            entry.browser_client,
            entry.resolvers,
            entry.reserves_funding,
        )
        for entry in entries
    } == {
        (
            "tenant.list_sources.get",
            "tenant",
            "list_sources",
            "GET",
            "session_or_pat",
            "viewer",
            True,
            (),
            False,
        ),
        (
            "tenant.get_source_ep.get",
            "tenant",
            "get_source_ep",
            "GET",
            "session_or_pat",
            "viewer",
            True,
            (),
            False,
        ),
        (
            "tenant.get_source_health_ep.get",
            "tenant",
            "get_source_health_ep",
            "GET",
            "session_or_pat",
            "viewer",
            True,
            (),
            False,
        ),
    }
    assert (
        not {
            entry.id
            for entry in BASE_ENDPOINT_CATALOG
            if entry.id.startswith("tenant.source_") and entry.browser_client
        }
        - OPERATION_IDS
    )


@pytest.mark.parametrize("name", sorted(ROUTES))
def test_source_reads_declare_exact_typed_route_shapes(
    name: str, routes_by_name: dict[str, APIRoute]
) -> None:
    assert set(routes_by_name) == set(ROUTES)
    route, truth = routes_by_name[name], ROUTES[name]
    assert route.methods == {"GET"}
    assert route.path == truth.path
    assert (route.status_code or 200) == 200
    response = route.response_model
    assert isinstance(response, type) and issubclass(response, BaseModel), (
        f"INTENDED_F4_RED: {name} must declare a typed response model"
    )
    assert response.__module__ == _CONTRACT_MODULE
    assert response.__name__ == truth.response
    assert set(route.responses) == set(truth.errors)
    assert all(value == {"model": HttpError} for value in route.responses.values())
    assert route.body_field is None
    assert route.dependant.body_params == []
    if truth.default_limit is None:
        assert route.dependant.query_params == []
        return
    query = {field.name: field for field in route.dependant.query_params}
    assert set(query) == {"runs_offset", "runs_limit"}
    assert query["runs_offset"].default == 0
    assert _query_metadata_bounds(query["runs_offset"]) == {"ge": 0}
    assert query["runs_limit"].default == truth.default_limit
    assert _query_metadata_bounds(query["runs_limit"]) == {"ge": 1, "le": 100}


def test_source_reads_continue_to_ignore_unknown_query_parameters(
    tmp_path: Path,
) -> None:
    """Typing the routes must not introduce a new query-key rejection policy."""

    with TestClient(create_app(tmp_path / "unknown-project-source-query")) as client:
        for path in (
            "/api/projects/missing/sources?unrecognized=1",
            "/api/projects/missing/sources/7?unrecognized=1",
            "/api/projects/missing/sources/7/health?unrecognized=1",
        ):
            response = client.get(path)
            assert response.status_code == 404


def test_project_source_dto_graph_freezes_every_current_raw_field() -> None:
    contracts = _contracts()
    source = _model(contracts, "ProjectSource")
    run = _model(contracts, "ProjectSourceRun")
    page = _model(contracts, "ProjectSourceRunsPage")
    detail = _model(contracts, "ProjectSourceDetail")
    health = _model(contracts, "ProjectSourceHealth")
    source_list = _model(contracts, "ProjectSourceList")

    assert issubclass(source, WireModel)
    for model in (source, run, page, detail, health):
        _assert_closed_wire_model(model)
        _assert_all_fields_required(model)

    source_annotations = {
        "id": int,
        "name": str,
        "kind": str,
        "url": str | None,
        "config": JsonValue,
        "sheet_id": int | None,
        "schedule": str | None,
        "enabled": bool,
        "cursor": str | None,
        "last_checked_at": str | None,
        "last_status": str | None,
        "new_rows_total": int,
        "created_at": str,
    }
    run_annotations = {
        "id": int,
        "source_id": int,
        "op_id": int | None,
        "receipt_id": str | None,
        "status": str,
        "new_rows": int,
        "skipped_rows": int,
        "changed_rows": int,
        "revisions": int,
        "error": str | None,
        "cursor_before": str | None,
        "cursor_after": str | None,
        "duration_ms": int | None,
        "warning_count": int,
        "cost_micro": int,
        "summary_json": str,
        "started_at": str,
        "finished_at": str | None,
    }
    page_annotations = {
        "schema_version": Literal["frisket.source_runs_page.v1"],
        "order": Literal["desc"],
        "offset": int,
        "limit": int,
        "total": int,
        "has_more": bool,
        "next_offset": int | None,
        "latest_run": run | None,
        "latest_run_loaded": bool,
    }

    assert {name: field.annotation for name, field in source.model_fields.items()} == (
        source_annotations
    )
    assert {name: field.annotation for name, field in run.model_fields.items()} == (
        run_annotations
    )
    assert {name: field.annotation for name, field in page.model_fields.items()} == (
        page_annotations
    )
    assert {name: field.annotation for name, field in detail.model_fields.items()} == {
        **source_annotations,
        "runs": list[run],
        "runs_page": page,
    }
    _assert_required_nullable(source, _SOURCE_NULLABLE_FIELDS)
    _assert_required_nullable(run, _RUN_NULLABLE_FIELDS)
    _assert_required_nullable(page, frozenset({"next_offset", "latest_run"}))

    assert set(source.model_fields) == _RAW_SOURCE_FIELDS
    assert set(run.model_fields) == _RAW_RUN_FIELDS
    assert set(page.model_fields) == _PAGE_FIELDS
    assert set(detail.model_fields) == _RAW_SOURCE_FIELDS | {"runs", "runs_page"}
    assert set(health.model_fields) == {
        "schema_version",
        "source",
        "summary",
        "runs_page",
        "runs",
        "downstream_jobs",
        "costs",
        "alerts",
        "warnings",
    }
    assert issubclass(source_list, RootModel)
    assert source_list.model_config.get("strict") is True
    source_list_root = source_list.model_fields["root"].annotation
    assert source_list_root == list[source]
    _assert_closed_annotation(source_list_root, label="ProjectSourceList.root")

    raw_source = {
        "id": 7,
        "name": "source",
        "kind": "rss",
        "url": "https://example.test/feed",
        "config": {"feed": "primary"},
        "sheet_id": 3,
        "schedule": "@hourly",
        "enabled": True,
        "cursor": "raw-source-cursor",
        "last_checked_at": None,
        "last_status": "never",
        "new_rows_total": 0,
        "created_at": "2026-08-10 00:00:00",
    }
    raw_run = {
        "id": 8,
        "source_id": 7,
        "op_id": None,
        "receipt_id": None,
        "status": "ok",
        "new_rows": 1,
        "skipped_rows": 2,
        "changed_rows": 3,
        "revisions": 4,
        "error": None,
        "cursor_before": "raw-before",
        "cursor_after": "raw-after",
        "duration_ms": 5,
        "warning_count": 6,
        "cost_micro": 7,
        "summary_json": '{"kept":"raw"}',
        "started_at": "2026-08-10 00:00:00",
        "finished_at": "2026-08-10 00:01:00",
    }
    assert source.model_validate(raw_source).model_dump() == raw_source
    for config in ({"nested": ["value"]}, [], "scalar", 7, True, None):
        assert source.model_validate({**raw_source, "config": config}).config == config
    for name in _SOURCE_NULLABLE_FIELDS:
        assert (
            source.model_validate({**raw_source, name: None}).model_dump()[name] is None
        )
    with pytest.raises(ValidationError):
        source.model_validate({**raw_source, "created_at": None})
    _assert_missing_fields_rejected(source, raw_source, _RAW_SOURCE_FIELDS)
    assert run.model_validate(raw_run).model_dump() == raw_run
    for name in _RUN_NULLABLE_FIELDS:
        assert run.model_validate({**raw_run, name: None}).model_dump()[name] is None
    with pytest.raises(ValidationError):
        run.model_validate({**raw_run, "started_at": None})
    _assert_missing_fields_rejected(run, raw_run, _RAW_RUN_FIELDS)
    assert source_list.model_validate([raw_source]).model_dump() == [raw_source]
    page_payload = {
        "schema_version": "frisket.source_runs_page.v1",
        "order": "desc",
        "offset": 0,
        "limit": 50,
        "total": 1,
        "has_more": False,
        "next_offset": None,
        "latest_run": raw_run,
        "latest_run_loaded": True,
    }
    assert page.model_validate(page_payload).model_dump() == page_payload
    assert (
        page.model_validate(
            {**page_payload, "latest_run": None, "latest_run_loaded": False}
        ).latest_run
        is None
    )
    with pytest.raises(ValidationError):
        page.model_validate(
            {**page_payload, "schema_version": "frisket.source_runs_page.v2"}
        )
    with pytest.raises(ValidationError):
        page.model_validate({**page_payload, "order": "asc"})
    _assert_missing_fields_rejected(page, page_payload, _PAGE_FIELDS)

    detail_payload = {**raw_source, "runs": [raw_run], "runs_page": page_payload}
    detail_wire = detail.model_validate(detail_payload)
    assert detail_wire.model_dump() == detail_payload
    assert isinstance(detail_wire.runs[0], run)
    assert isinstance(detail_wire.runs_page, page)
    assert isinstance(detail_wire.runs_page.latest_run, run)
    _assert_missing_fields_rejected(
        detail,
        detail_payload,
        _RAW_SOURCE_FIELDS | frozenset({"runs", "runs_page"}),
    )


def test_health_dto_is_explicit_and_keeps_redaction_separate_from_raw_reads() -> None:
    contracts = _contracts()
    health = _model(contracts, "ProjectSourceHealth")
    source = _model(contracts, "ProjectSourceHealthSource")
    summary = _model(contracts, "ProjectSourceHealthSummary")
    page = _model(contracts, "ProjectSourceHealthRunsPage")
    run = _model(contracts, "ProjectSourceHealthRun")
    job = _model(contracts, "ProjectSourceHealthJob")
    refs = _model(contracts, "ProjectSourceHealthJobRefs")
    costs = _model(contracts, "ProjectSourceHealthCosts")
    sparse_notice = _model(contracts, "ProjectSourceHealthSparseRunNotice")
    unsupported_schedule_notice = _model(
        contracts, "ProjectSourceHealthUnsupportedScheduleNotice"
    )

    assert set(source.model_fields) == _HEALTH_SOURCE_FIELDS
    assert set(summary.model_fields) == _HEALTH_SUMMARY_FIELDS
    assert set(page.model_fields) == _HEALTH_PAGE_FIELDS
    assert set(run.model_fields) == _HEALTH_RUN_FIELDS
    assert set(job.model_fields) == _HEALTH_JOB_FIELDS
    assert set(refs.model_fields) == _HEALTH_REFS_FIELDS
    assert set(sparse_notice.model_fields) == _HEALTH_SPARSE_NOTICE_FIELDS
    assert set(unsupported_schedule_notice.model_fields) == (
        _HEALTH_UNSUPPORTED_SCHEDULE_NOTICE_FIELDS
    )
    assert set(costs.model_fields) == {
        "recent_actual_micro",
        "recent_estimated_micro",
        "basis",
    }
    for model in (
        health,
        source,
        summary,
        page,
        run,
        job,
        refs,
        costs,
        sparse_notice,
        unsupported_schedule_notice,
    ):
        _assert_closed_wire_model(model)

    source_annotations = {
        "id": int,
        "name": str,
        "kind": str,
        "url": str | None,
        "enabled": bool,
        "schedule": str | None,
        "sheet_id": int | None,
        "created_at": str,
        "redactions": list[str],
    }
    summary_annotations = {
        "status": Literal["disabled", "never_run", "failing", "stale", "healthy"],
        "last_success_at": str | None,
        "last_failure_at": str | None,
        "consecutive_failures": int,
        "new_rows_total": int,
        "new_rows_recent": int,
        "changed_rows_recent": int,
        "skipped_rows_recent": int,
        "revisions_recent": int,
        "recent_run_count": int,
        "last_cursor_summary": Literal[
            "stored",
            "run_cursor_available",
            "run_cursor_before_only",
            "not_recorded",
        ],
    }
    page_annotations = {
        "schema_version": Literal["frisket.source_runs_page.v1"],
        "order": Literal["desc"],
        "offset": int,
        "limit": int,
        "total": int,
        "has_more": bool,
        "next_offset": int | None,
    }
    run_annotations = {
        "id": int,
        "source_id": int,
        "status": str,
        "started_at": str,
        "finished_at": str | None,
        "receipt_id": str | None,
        "op_id": int | None,
        "new_rows": int,
        "skipped_rows": int,
        "changed_rows": int,
        "revisions": int,
        "duration_ms": int | None,
        "warning_count": int,
        "cost_micro": int,
        "error_summary": str | None,
        "cursor_before_present": bool,
        "cursor_after_present": bool,
        "summary_present": bool,
    }
    refs_annotations = {
        "source_id": int,
        "source_run_id": int | None,
        "sheet_id": int | None,
    }
    job_annotations = {
        "job_id": int,
        "kind": str,
        "status": str,
        "attempts": int,
        "max_attempts": int,
        "created_at": str,
        "started_at": str | None,
        "finished_at": str | None,
        "refs": refs,
        "result_summary": dict[str, JsonValue],
        "error_summary": str | None,
        "stalled": bool,
    }
    costs_annotations = {
        "recent_actual_micro": int,
        "recent_estimated_micro": int,
        "basis": str,
    }
    sparse_notice_annotations = {
        "code": Literal["sparse_source_run_metadata"],
        "message": str,
        "run_ids": list[int],
    }
    unsupported_schedule_notice_annotations = {
        "code": Literal["unsupported_source_schedule"],
        "message": str,
    }
    health_annotations = {
        "schema_version": Literal["frisket.source_health.v1"],
        "source": source,
        "summary": summary,
        "runs_page": page,
        "runs": list[run],
        "downstream_jobs": list[job],
        "costs": costs,
        "alerts": list[sparse_notice | unsupported_schedule_notice],
        "warnings": list[sparse_notice | unsupported_schedule_notice],
    }
    for model, expected in (
        (source, source_annotations),
        (summary, summary_annotations),
        (page, page_annotations),
        (run, run_annotations),
        (refs, refs_annotations),
        (job, job_annotations),
        (costs, costs_annotations),
        (sparse_notice, sparse_notice_annotations),
        (unsupported_schedule_notice, unsupported_schedule_notice_annotations),
        (health, health_annotations),
    ):
        assert {
            name: field.annotation for name, field in model.model_fields.items()
        } == (expected)

    for model in (health, source, summary, page, run, job, costs):
        _assert_all_fields_required(model)
    _assert_required_nullable(source, _HEALTH_SOURCE_NULLABLE_FIELDS)
    _assert_required_nullable(summary, _HEALTH_SUMMARY_NULLABLE_FIELDS)
    _assert_required_nullable(page, _HEALTH_PAGE_NULLABLE_FIELDS)
    _assert_required_nullable(run, _HEALTH_RUN_NULLABLE_FIELDS)
    _assert_required_nullable(job, _HEALTH_JOB_NULLABLE_FIELDS)
    assert refs.model_fields["source_id"].is_required()
    _assert_optional_nullable(refs, "source_run_id")
    _assert_optional_nullable(refs, "sheet_id")
    _assert_all_fields_required(sparse_notice)
    _assert_all_fields_required(unsupported_schedule_notice)

    payload = {
        "schema_version": "frisket.source_health.v1",
        "source": {
            "id": 7,
            "name": "source",
            "kind": "rss",
            "url": None,
            "enabled": True,
            "schedule": None,
            "sheet_id": None,
            "created_at": "2026-08-10 00:00:00",
            "redactions": ["config", "cursor"],
        },
        "summary": {
            "status": "healthy",
            "last_success_at": None,
            "last_failure_at": None,
            "consecutive_failures": 0,
            "new_rows_total": 0,
            "new_rows_recent": 0,
            "changed_rows_recent": 0,
            "skipped_rows_recent": 0,
            "revisions_recent": 0,
            "recent_run_count": 1,
            "last_cursor_summary": "stored",
        },
        "runs_page": {
            "schema_version": "frisket.source_runs_page.v1",
            "order": "desc",
            "offset": 0,
            "limit": 20,
            "total": 1,
            "has_more": False,
            "next_offset": None,
        },
        "runs": [
            {
                "id": 8,
                "source_id": 7,
                "status": "running",
                "started_at": "2026-08-10 00:00:00",
                "finished_at": None,
                "receipt_id": None,
                "op_id": None,
                "new_rows": 0,
                "skipped_rows": 0,
                "changed_rows": 0,
                "revisions": 0,
                "duration_ms": None,
                "warning_count": 0,
                "cost_micro": 0,
                "error_summary": None,
                "cursor_before_present": True,
                "cursor_after_present": False,
                "summary_present": False,
            }
        ],
        "downstream_jobs": [
            {
                "job_id": 9,
                "kind": "source.poll",
                "status": "done",
                "attempts": 1,
                "max_attempts": 3,
                "created_at": "2026-08-10T00:00:00Z",
                "started_at": None,
                "finished_at": None,
                "refs": {"source_id": 7},
                "result_summary": {"kept": [1]},
                "error_summary": None,
                "stalled": False,
            }
        ],
        "costs": {
            "recent_actual_micro": 0,
            "recent_estimated_micro": 0,
            "basis": "source_runs.cost_micro for loaded runs; downstream costs unknown-safe",
        },
        "alerts": [
            {
                "code": "unsupported_source_schedule",
                "message": "Source schedule could not be parsed for stale detection.",
            }
        ],
        "warnings": [
            {
                "code": "sparse_source_run_metadata",
                "message": "Some loaded source runs lack v1 runtime metadata; row deltas and costs may be incomplete.",
                "run_ids": [8],
            },
        ],
    }
    health_wire = health.model_validate(payload)
    assert health_wire.model_dump(exclude_unset=True) == payload
    assert isinstance(health_wire.source, source)
    assert isinstance(health_wire.summary, summary)
    assert isinstance(health_wire.runs_page, page)
    assert isinstance(health_wire.runs[0], run)
    assert isinstance(health_wire.downstream_jobs[0], job)
    assert isinstance(health_wire.downstream_jobs[0].refs, refs)
    assert isinstance(health_wire.costs, costs)
    assert isinstance(health_wire.alerts[0], unsupported_schedule_notice)
    assert isinstance(health_wire.warnings[0], sparse_notice)
    assert (
        unsupported_schedule_notice.model_validate(payload["alerts"][0]).model_dump()
        == payload["alerts"][0]
    )
    assert (
        sparse_notice.model_validate(payload["warnings"][0]).model_dump()
        == payload["warnings"][0]
    )
    assert refs.model_validate({"source_id": 7}).model_dump(exclude_unset=True) == {
        "source_id": 7
    }

    with pytest.raises(ValidationError):
        health.model_validate({**payload, "schema_version": "frisket.source_health.v2"})
    with pytest.raises(ValidationError):
        health.model_validate(
            {**payload, "summary": {**payload["summary"], "status": "unknown"}}
        )
    with pytest.raises(ValidationError):
        health.model_validate(
            {
                **payload,
                "summary": {
                    **payload["summary"],
                    "last_cursor_summary": "unrecognized_cursor_state",
                },
            }
        )
    with pytest.raises(ValidationError):
        health.model_validate(
            {
                **payload,
                "runs_page": {
                    **payload["runs_page"],
                    "order": "asc",
                },
            }
        )
    with pytest.raises(ValidationError):
        health.model_validate(
            {
                **payload,
                "runs_page": {
                    **payload["runs_page"],
                    "schema_version": "frisket.source_runs_page.v2",
                },
            }
        )
    with pytest.raises(ValidationError):
        health.model_validate(
            {**payload, "source": {**payload["source"], "created_at": None}}
        )
    with pytest.raises(ValidationError):
        health.model_validate(
            {
                **payload,
                "downstream_jobs": [
                    {**payload["downstream_jobs"][0], "created_at": None}
                ],
            }
        )

    _assert_missing_fields_rejected(health, payload, frozenset(health_annotations))
    _assert_missing_fields_rejected(source, payload["source"], _HEALTH_SOURCE_FIELDS)
    _assert_missing_fields_rejected(summary, payload["summary"], _HEALTH_SUMMARY_FIELDS)
    _assert_missing_fields_rejected(page, payload["runs_page"], _HEALTH_PAGE_FIELDS)
    _assert_missing_fields_rejected(run, payload["runs"][0], _HEALTH_RUN_FIELDS)
    _assert_missing_fields_rejected(
        job, payload["downstream_jobs"][0], _HEALTH_JOB_FIELDS
    )
    _assert_missing_fields_rejected(
        costs, payload["costs"], frozenset(costs_annotations)
    )
    _assert_missing_fields_rejected(
        sparse_notice, payload["warnings"][0], _HEALTH_SPARSE_NOTICE_FIELDS
    )
    _assert_missing_fields_rejected(
        unsupported_schedule_notice,
        payload["alerts"][0],
        _HEALTH_UNSUPPORTED_SCHEDULE_NOTICE_FIELDS,
    )

    with pytest.raises(ValidationError):
        health.model_validate(
            {**payload, "source": {**payload["source"], "config": {}}}
        )
    with pytest.raises(ValidationError):
        health.model_validate(
            {
                **payload,
                "downstream_jobs": [
                    {**payload["downstream_jobs"][0], "refs": {"source_id": "7"}}
                ],
            }
        )
    with pytest.raises(ValidationError):
        health.model_validate(
            {
                **payload,
                "alerts": [
                    {
                        **payload["warnings"][0],
                        "unapproved_notice_field": True,
                    }
                ],
            }
        )
    with pytest.raises(ValidationError):
        health.model_validate(
            {
                **payload,
                "downstream_jobs": [
                    {
                        **payload["downstream_jobs"][0],
                        "refs": {"source_id": 7, "unapproved_ref": 9},
                    }
                ],
            }
        )
    with pytest.raises(ValidationError):
        health.model_validate(
            {
                **payload,
                "downstream_jobs": [
                    {**payload["downstream_jobs"][0], "result_summary": [1]}
                ],
            }
        )
    with pytest.raises(ValidationError):
        health.model_validate(
            {
                **payload,
                "warnings": [
                    {
                        **payload["warnings"][0],
                        "unapproved_notice_field": True,
                    }
                ],
            }
        )
    with pytest.raises(ValidationError):
        sparse_notice.model_validate(
            {"code": "sparse_source_run_metadata", "run_ids": [8]}
        )
    with pytest.raises(ValidationError):
        sparse_notice.model_validate(
            {
                **payload["warnings"][0],
                "run_ids": ["8"],
            }
        )
    with pytest.raises(ValidationError):
        unsupported_schedule_notice.model_validate(
            {**payload["alerts"][0], "run_ids": [8]}
        )
    with pytest.raises(ValidationError):
        health.model_validate(
            {
                **payload,
                "alerts": [
                    {
                        "code": "unrecognized_source_health_notice",
                        "message": "not a current producer notice",
                    }
                ],
            }
        )


def test_live_source_reads_preserve_raw_and_redacted_shapes(
    tmp_path: Path,
) -> None:
    """The typed decorators validate existing list/detail/health producers."""

    app = create_app(tmp_path / "live-project-source-reads")
    workspace = app.state.workspace
    project_id = str(workspace.create("live project source reads")["id"])
    project = workspace.get(project_id)
    store = SourceStore(project)
    config_cases = (
        ("object", {"nested": ["value"]}),
        ("array", ["value", 7]),
        ("string", "scalar"),
        ("number", 7),
        ("boolean", True),
        ("null", None),
    )
    source_ids: list[int] = []
    for label, config in config_cases:
        source_id = store.add_source(
            f"config-{label}",
            kind="rss",
            url=None,
            sheet_id=None,
            schedule=None,
        )
        project.db.execute(
            "UPDATE sources SET config=? WHERE id=?",
            (json.dumps(config), source_id),
        )
        source_ids.append(source_id)

    detailed_source_id = source_ids[0]
    project.db.execute(
        "UPDATE sources SET cursor=?, last_checked_at=?, last_status=? WHERE id=?",
        ("raw-source-cursor", None, None, detailed_source_id),
    )
    project.db.commit()
    run_id = store.start_source_run(detailed_source_id, cursor_before="raw-before")
    job_id = workspace.queue.enqueue(
        "source.poll",
        {"project_id": project_id, "source_id": detailed_source_id},
    )

    with TestClient(app) as client:
        listed_response = client.get(f"/api/projects/{project_id}/sources")
        assert listed_response.status_code == 200
        listed = listed_response.json()
        assert [row["id"] for row in listed] == source_ids
        assert [row["config"] for row in listed] == [
            config for _, config in config_cases
        ]
        assert all(set(row) == _RAW_SOURCE_FIELDS for row in listed)
        assert listed[0]["url"] is None
        assert listed[0]["sheet_id"] is None
        assert listed[0]["schedule"] is None
        assert listed[0]["last_checked_at"] is None
        assert listed[0]["last_status"] is None

        for source_id, (_, config) in zip(source_ids, config_cases, strict=True):
            detail_response = client.get(
                f"/api/projects/{project_id}/sources/{source_id}"
            )
            assert detail_response.status_code == 200
            assert detail_response.json()["config"] == config

        detail_response = client.get(
            f"/api/projects/{project_id}/sources/{detailed_source_id}"
        )
        assert detail_response.status_code == 200
        detail = detail_response.json()
        assert set(detail) == _RAW_SOURCE_FIELDS | {"runs", "runs_page"}
        assert {name: detail[name] for name in _RAW_SOURCE_FIELDS} == listed[0]
        assert detail["cursor"] == "raw-source-cursor"
        assert isinstance(detail["created_at"], str)
        assert len(detail["runs"]) == 1
        raw_run = detail["runs"][0]
        assert set(raw_run) == _RAW_RUN_FIELDS
        assert {
            "id": raw_run["id"],
            "source_id": raw_run["source_id"],
            "op_id": raw_run["op_id"],
            "receipt_id": raw_run["receipt_id"],
            "status": raw_run["status"],
            "new_rows": raw_run["new_rows"],
            "skipped_rows": raw_run["skipped_rows"],
            "changed_rows": raw_run["changed_rows"],
            "revisions": raw_run["revisions"],
            "error": raw_run["error"],
            "cursor_before": raw_run["cursor_before"],
            "cursor_after": raw_run["cursor_after"],
            "duration_ms": raw_run["duration_ms"],
            "warning_count": raw_run["warning_count"],
            "cost_micro": raw_run["cost_micro"],
            "summary_json": raw_run["summary_json"],
            "finished_at": raw_run["finished_at"],
        } == {
            "id": run_id,
            "source_id": detailed_source_id,
            "op_id": None,
            "receipt_id": None,
            "status": "running",
            "new_rows": 0,
            "skipped_rows": 0,
            "changed_rows": 0,
            "revisions": 0,
            "error": None,
            "cursor_before": "raw-before",
            "cursor_after": None,
            "duration_ms": None,
            "warning_count": 0,
            "cost_micro": 0,
            "summary_json": "{}",
            "finished_at": None,
        }
        assert isinstance(raw_run["started_at"], str)
        assert detail["runs_page"] == {
            "schema_version": "frisket.source_runs_page.v1",
            "order": "desc",
            "offset": 0,
            "limit": 50,
            "total": 1,
            "has_more": False,
            "next_offset": None,
            "latest_run": raw_run,
            "latest_run_loaded": True,
        }

        health_response = client.get(
            f"/api/projects/{project_id}/sources/{detailed_source_id}/health"
        )
        assert health_response.status_code == 200
        health = health_response.json()
        assert set(health) == {
            "schema_version",
            "source",
            "summary",
            "runs_page",
            "runs",
            "downstream_jobs",
            "costs",
            "alerts",
            "warnings",
        }
        assert set(health["source"]) == _HEALTH_SOURCE_FIELDS
        assert "config" not in health["source"]
        assert "cursor" not in health["source"]
        assert health["source"] == {
            "id": detailed_source_id,
            "name": "config-object",
            "kind": "rss",
            "url": None,
            "enabled": True,
            "schedule": None,
            "sheet_id": None,
            "created_at": detail["created_at"],
            "redactions": ["config", "cursor"],
        }
        assert health["summary"] == {
            "status": "healthy",
            "last_success_at": None,
            "last_failure_at": None,
            "consecutive_failures": 0,
            "new_rows_total": 0,
            "new_rows_recent": 0,
            "changed_rows_recent": 0,
            "skipped_rows_recent": 0,
            "revisions_recent": 0,
            "recent_run_count": 1,
            "last_cursor_summary": "stored",
        }
        assert health["runs_page"] == {
            "schema_version": "frisket.source_runs_page.v1",
            "order": "desc",
            "offset": 0,
            "limit": 20,
            "total": 1,
            "has_more": False,
            "next_offset": None,
        }
        assert health["runs"] == [
            {
                "id": run_id,
                "source_id": detailed_source_id,
                "status": "running",
                "started_at": raw_run["started_at"],
                "finished_at": None,
                "receipt_id": None,
                "op_id": None,
                "new_rows": 0,
                "skipped_rows": 0,
                "changed_rows": 0,
                "revisions": 0,
                "duration_ms": None,
                "warning_count": 0,
                "cost_micro": 0,
                "error_summary": None,
                "cursor_before_present": True,
                "cursor_after_present": False,
                "summary_present": False,
            }
        ]
        assert len(health["downstream_jobs"]) == 1
        job = health["downstream_jobs"][0]
        assert set(job) == _HEALTH_JOB_FIELDS
        assert job == {
            "job_id": job_id,
            "kind": "source.poll",
            "status": "queued",
            "attempts": 0,
            "max_attempts": 3,
            "created_at": job["created_at"],
            "started_at": None,
            "finished_at": None,
            "refs": {"source_id": detailed_source_id},
            "result_summary": {},
            "error_summary": None,
            "stalled": False,
        }
        assert isinstance(job["created_at"], str)
        assert health["costs"] == {
            "recent_actual_micro": 0,
            "recent_estimated_micro": 0,
            "basis": "source_runs.cost_micro for loaded runs; downstream costs unknown-safe",
        }
        assert health["alerts"] == []
        assert health["warnings"] == [
            {
                "code": "sparse_source_run_metadata",
                "message": "Some loaded source runs lack v1 runtime metadata; row deltas and costs may be incomplete.",
                "run_ids": [run_id],
            }
        ]
        for path in (
            f"/api/projects/{project_id}/sources?unrecognized=1",
            f"/api/projects/{project_id}/sources/{detailed_source_id}?unrecognized=1",
            f"/api/projects/{project_id}/sources/{detailed_source_id}/health?unrecognized=1",
        ):
            assert client.get(path).status_code == 200


def test_source_read_error_oracles_preserve_existing_http_behavior(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path / "project-source-read-errors")
    workspace = app.state.workspace
    project_id = str(workspace.create("source read errors")["id"])
    project = workspace.get(project_id)
    source_id = SourceStore(project).add_source("source")
    missing_source_id = source_id + 100

    with TestClient(app) as client:
        for suffix in ("", "/1", "/1/health"):
            response = client.get(f"/api/projects/missing/sources{suffix}")
            assert response.status_code == 404
            assert response.json() == {"detail": "no project 'missing'"}

        for suffix in ("", "/health"):
            response = client.get(
                f"/api/projects/{project_id}/sources/{missing_source_id}{suffix}"
            )
            assert response.status_code == 404
            assert response.json() == {"detail": "source not found"}

        detail_bad_offset = client.get(
            f"/api/projects/{project_id}/sources/{source_id}?runs_offset=-1"
        )
        assert detail_bad_offset.status_code == 422
        assert detail_bad_offset.json() == {
            "detail": [
                {
                    "type": "greater_than_equal",
                    "loc": ["query", "runs_offset"],
                    "msg": "Input should be greater than or equal to 0",
                    "input": "-1",
                    "ctx": {"ge": 0},
                }
            ]
        }

        health_bad_limit = client.get(
            f"/api/projects/{project_id}/sources/{source_id}/health?runs_limit=101"
        )
        assert health_bad_limit.status_code == 422
        assert health_bad_limit.json() == {
            "detail": [
                {
                    "type": "less_than_equal",
                    "loc": ["query", "runs_limit"],
                    "msg": "Input should be less than or equal to 100",
                    "input": "101",
                    "ctx": {"le": 100},
                }
            ]
        }
