"""Semantic matching, source ownership, review ordering and linked materialization."""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.semantic_join import SemanticJoinParams
from frisket.actions.types import ActionRequest, SheetColumnRef
from frisket.ai.llm import ModelRouter
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.semantic_join_matcher import AdmittedSemanticMatcher
from frisket.engine.executor.semantic_join_program import semantic_join_references
from frisket.ops.base import OpContext
from frisket.semantic import resolve_embedder as _REAL_RESOLVE_EMBEDDER
from frisket.engine.runner.review import review_queue
from frisket.engine.store import Project
from http_test_helpers import drain_queue

VECS = {
    "Acme Corporation": [1.0, 0.0, 0.0],
    "Globex LLC": [0.0, 1.0, 0.0],
    "Initech Inc": [0.0, 0.0, 1.0],
    "ACME Corp": [1.0, 0.0, 0.0],
    "Globex": [0.6258, 0.78, 0.0],
    "Umbrella Holdings": [0.5774, 0.5774, 0.5774],
}


def _fake_embed(texts):
    return [VECS[text] for text in texts]


@pytest.fixture(autouse=True)
def _stub_embedder(monkeypatch):
    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder",
        lambda router=None, **_kw: (_fake_embed, "stub/test-v1"),
    )


def _request(
    source_sheet_id,
    target_sheet_id,
    *,
    source="donor",
    target_column="company",
    carry=(),
    sheet_name="Matches",
    output_names=None,
    row_ids=None,
    match_threshold=0.70,
    confident_threshold=0.85,
    idempotency_key="semantic-join-test",
):
    return {
        "action_id": "join.semantic",
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": source_sheet_id,
            **({"row_ids": row_ids} if row_ids is not None else {}),
        },
        "params": {
            "source": source,
            "target": {"sheet_id": target_sheet_id, "column": target_column},
            "carry": list(carry),
            "match_threshold": match_threshold,
            "confident_threshold": confident_threshold,
        },
        "sheet_name": sheet_name,
        "output_names": output_names or {},
        "idempotency_key": idempotency_key,
    }


def _run_confirmed(project, router, request):
    gated = run_action_spec(
        project, request, project_id="semantic-join-unit", router=router
    )
    assert gated.status == "needs_confirmation", gated.errors
    token = gated.errors[0].details["promise_set_hash"]
    assert isinstance(token, str) and token
    return run_action_spec(
        project,
        {**request, "confirmation": token},
        project_id="semantic-join-unit",
        router=router,
    )


def _project(tmp_path):
    p = Project.create(tmp_path / "t.frisket", name="t")
    a = p.add_sheet("donors")
    acols = {"donor": p.add_column(a, "donor", type="text")}
    a_rows = p.add_rows(
        a,
        [
            {"donor": "ACME Corp"},
            {"donor": "Globex"},
            {"donor": "Umbrella Holdings"},
        ],
        acols,
    )
    b = p.add_sheet("registry")
    bcols = {"company": p.add_column(b, "company", type="text")}
    b_rows = p.add_rows(
        b,
        [
            {"company": "Acme Corporation"},
            {"company": "Globex LLC"},
            {"company": "Initech Inc"},
        ],
        bcols,
    )
    return p, a, b, a_rows, b_rows


def _project_with_location(tmp_path):
    """Like _project, but sheet A also carries a 'location' column — the
    fixture for 'columns to carry over' (design card 5b) tests below."""
    p = Project.create(tmp_path / "t.frisket", name="t")
    a = p.add_sheet("donors")
    acols = {
        "donor": p.add_column(a, "donor", type="text"),
        "location": p.add_column(a, "location", type="text"),
    }
    a_rows = p.add_rows(
        a,
        [
            {"donor": "ACME Corp", "location": "Boston"},
            {"donor": "Globex", "location": "NYC"},
            {"donor": "Umbrella Holdings", "location": "LA"},
        ],
        acols,
    )
    b = p.add_sheet("registry")
    bcols = {"company": p.add_column(b, "company", type="text")}
    b_rows = p.add_rows(
        b,
        [
            {"company": "Acme Corporation"},
            {"company": "Globex LLC"},
            {"company": "Initech Inc"},
        ],
        bcols,
    )
    return p, a, b, a_rows, b_rows


def _client(tmp_path) -> TestClient:
    from frisket.server.app import create_app

    return TestClient(
        create_app(
            tmp_path / "workspace",
            router=ModelRouter(cache=None, cache_mode="off"),
        )
    )


def _api_project(client: TestClient) -> tuple[str, int, int]:
    pid = client.post("/api/projects", json={"name": "Join API"}).json()["id"]
    donors = client.post(
        f"/api/projects/{pid}/import/csv",
        files={
            "file": (
                "donors.csv",
                "donor\nACME Corp\nGlobex\nUmbrella Holdings\n",
                "text/csv",
            )
        },
    )
    registry = client.post(
        f"/api/projects/{pid}/import/csv",
        files={
            "file": (
                "registry.csv",
                "company\nAcme Corporation\nGlobex LLC\nInitech Inc\n",
                "text/csv",
            )
        },
    )
    assert donors.status_code == 200, donors.text
    assert registry.status_code == 200, registry.text
    return pid, donors.json()["sheet_id"], registry.json()["sheet_id"]


def _match(
    project,
    target_sheet_id,
    value,
    *,
    target_column="company",
    match_threshold=0.70,
    confident_threshold=0.85,
    backend=None,
):
    """Exercise the real admitted matcher and vector cache with a local fixture."""
    target = SheetColumnRef(sheet_id=target_sheet_id, column=target_column)
    column = next(
        c for c in project.columns(target_sheet_id) if c["name"] == target_column
    )
    backend = backend or (_fake_embed, "fastembed/unit-fixture")
    admitted = AdmittedSemanticMatcher(
        target=target,
        values=tuple(project.get_values(target_sheet_id, column["id"]).items()),
        backend=backend,
        estimated_model=backend[1],
    )
    bound = admitted.bind_row(OpContext(project=project), expected_source=value)
    result = asyncio.run(
        bound.match(
            value,
            target=target,
            match_threshold=match_threshold,
            confident_threshold=confident_threshold,
        )
    )
    bound.validate_output(result)
    return result


def test_registered_and_output_fields():
    registered = ACTION_REGISTRY.get("join.semantic")
    params, outputs = registered.bind_request(
        ActionRequest.model_validate(_request(1, 2))
    )
    assert isinstance(params, SemanticJoinParams)
    assert [(field.key, field.column_type) for field in outputs] == [
        ("match_value", "text"),
        ("match_score", "number"),
        ("matched_row_id", "integer"),
    ]
    assert set(SemanticJoinParams.model_fields) == {
        "source",
        "target",
        "carry",
        "match_threshold",
        "confident_threshold",
    }
    entry = registered.catalog_entry()
    assert entry["required_capabilities"] == ["project:write", "model:embed"]
    assert entry["ui_hints"]["typed_action"]["creates_sheet"] is True


def test_typed_references_are_the_only_source_authority():
    registered = ACTION_REGISTRY.get("join.semantic")
    params = SemanticJoinParams(
        source="company",
        target={"sheet_id": 2, "column": "company"},
        carry=["location"],
    )
    source, target, carry = semantic_join_references(registered.definition.run, params)
    assert source.name == "company"
    assert target == SheetColumnRef(sheet_id=2, column="company")
    assert [ref.name for ref in carry] == ["location"]
    assert params.model_dump(mode="json") == {
        "source": "company",
        "target": {"sheet_id": 2, "column": "company"},
        "carry": ["location"],
        "match_threshold": 0.70,
        "confident_threshold": 0.85,
    }


@pytest.mark.parametrize("carry", [[" "], ["location", "location"], ["donor"]])
def test_join_semantic_params_rejects_bad_carry_columns(carry):
    with pytest.raises(ValidationError):
        SemanticJoinParams(
            source="donor", target={"sheet_id": 2, "column": "company"}, carry=carry
        )


def test_omitted_and_explicit_empty_carry_are_valid_and_no_source_is_manufactured():
    for options in ({}, {"carry": []}):
        params = SemanticJoinParams(
            source="donor", target={"sheet_id": 2, "column": "company"}, **options
        )
        assert params.carry == []


@pytest.mark.parametrize("low,high", [(0.9, 0.8), (-0.1, 0.8), (0.7, 1.1), (0.7, 0.7)])
def test_threshold_validation(low, high):
    with pytest.raises(ValidationError):
        SemanticJoinParams(
            source="donor",
            target={"sheet_id": 2, "column": "company"},
            match_threshold=low,
            confident_threshold=high,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"target_sheet_id": 999},
        {"target_column": "nope"},
        {"source": "company"},
        {"carry": ["nope"]},
    ],
)
def test_missing_sources_refuse_before_embedding_or_mutation(
    tmp_path, monkeypatch, change
):
    p, a, b, *_ = _project_with_location(tmp_path)
    calls = []
    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder",
        lambda *args, **kwargs: (lambda texts: calls.append(texts), "stub/test"),
    )
    target = change.pop("target_sheet_id", b)
    try:
        result = run_action_spec(p, _request(a, target, **change), project_id="test")
        assert result.status == "failed"
        assert result.errors
        assert calls == []
        assert p.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert len(p.sheets()) == 2
    finally:
        p.close()


def test_self_join_rejected(tmp_path):
    p, a, *_ = _project(tmp_path)
    try:
        result = run_action_spec(
            p, _request(a, a, target_column="donor"), project_id="test"
        )
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_params"
        assert "different" in result.errors[0].details["errors"][0]["message"].lower()
        assert len(p.sheets()) == 2
    finally:
        p.close()


def test_no_embedding_backend_fails_honestly(tmp_path, monkeypatch):
    p, a, b, *_ = _project(tmp_path)
    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder", lambda *args, **kwargs: None
    )
    try:
        result = run_action_spec(p, _request(a, b), project_id="test")
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_params"
        assert "embedding backend" in result.errors[0].details["errors"][0]["message"]
        assert p.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        p.close()


@pytest.mark.network
@pytest.mark.real
def test_local_embedder_path_smoke(tmp_path, monkeypatch):
    from frisket.semantic import local_embedder

    backend = local_embedder()
    if backend is None:
        pytest.skip("local semantic embedder unavailable")
    p = Project.create(tmp_path / "real.frisket")
    try:
        sheet = p.add_sheet("categories")
        column = p.add_column(sheet, "label", type="text")
        rows = p.add_rows(
            sheet,
            [{"label": "car trouble"}, {"label": "banana bread recipe"}],
            {"label": column},
        )
        good = _match(
            p,
            sheet,
            "the automobile would not start",
            target_column="label",
            match_threshold=0.35,
            confident_threshold=0.95,
            backend=backend,
        )
        assert good.match_value.value == "car trouble"
        assert good.matched_row_id.value == rows[0]
        assert good.match_score.value > 0.35
        bad = _match(
            p,
            sheet,
            "a quantum mechanics lecture",
            target_column="label",
            match_threshold=0.80,
            confident_threshold=0.95,
            backend=backend,
        )
        assert bad.match_value.value is None
        assert bad.matched_row_id.value is None
    finally:
        p.close()


@pytest.mark.parametrize(
    "value,index,score,justification",
    [
        ("ACME Corp", 0, 1.0, None),
        ("Globex", 1, 0.78, "gray-band"),
        ("Umbrella Holdings", None, 0.5774, "no match >= 0.7"),
    ],
)
def test_confident_gray_and_below_threshold_matching(
    tmp_path, value, index, score, justification
):
    p, _, b, _, target_rows = _project(tmp_path)
    try:
        result = _match(p, b, value)
        assert result.match_score.value == pytest.approx(score, abs=0.005)
        assert result.match_value.confidence == pytest.approx(score, abs=0.005)
        assert result.matched_row_id.value == (
            target_rows[index] if index is not None else None
        )
        if justification is None:
            assert result.match_value.justification is None
            assert result.match_value.value == "Acme Corporation"
        else:
            assert justification in result.match_value.justification
            assert result.match_value.value == ("Globex LLC" if index == 1 else None)
    finally:
        p.close()


def test_empty_cell_matches_nothing(tmp_path):
    p, _, b, *_ = _project(tmp_path)
    try:
        out = _match(p, b, None)
        assert out.match_value.value is None
        assert out.match_score.value is None
        assert out.matched_row_id.value is None
        assert p.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
    finally:
        p.close()


def test_runner_end_to_end_gray_band_surfaces_first(tmp_path):
    p, a, b, a_rows, b_rows = _project(tmp_path)
    try:
        result = _run_confirmed(
            p, ModelRouter(cache=None, cache_mode="off"), _request(a, b)
        )
        assert result.status == "completed", result.errors
        cols = {c["name"]: c for c in p.columns(a)}
        assert [
            cols[name]["type"]
            for name in ("match_value", "match_score", "matched_row_id")
        ] == ["text", "number", "integer"]
        matched = p.get_values(a, cols["matched_row_id"]["id"])
        assert [matched[row] for row in a_rows] == [b_rows[0], b_rows[1], None]
        queue = review_queue(p, sheet_id=a)
        assert queue[0]["row_id"] == a_rows[2]
        assert queue[0]["confidence"] == pytest.approx(0.5774, abs=0.005)
        assert [item["confidence"] for item in queue] == sorted(
            item["confidence"] for item in queue
        )
        gray = [
            item
            for item in queue
            if item["row_id"] == a_rows[1] and item["column_name"] == "match_value"
        ]
        assert gray and "gray-band" in gray[0]["justification"]
    finally:
        p.close()


@pytest.mark.parametrize("carry", [[], ["location"]])
def test_materialization_keeps_parent_lineage_and_exact_source_carry(tmp_path, carry):
    p, a, b, a_rows, b_rows = _project_with_location(tmp_path)
    names = {"source": "donor", **({"carry.location": "location"} if carry else {})}
    try:
        result = _run_confirmed(
            p,
            ModelRouter(cache=None, cache_mode="off"),
            _request(a, b, carry=carry, sheet_name="linkage", output_names=names),
        )
        assert result.status == "completed", result.errors
        child = next(s for s in p.sheets() if s["name"] == "linkage")
        assert child["parent_sheet_id"] == a
        rows = p.db.execute(
            "SELECT id,parent_row_id FROM rows WHERE sheet_id=? ORDER BY position",
            (child["id"],),
        ).fetchall()
        assert [row["parent_row_id"] for row in rows] == a_rows[:2]
        cols = {c["name"]: c for c in p.columns(child["id"])}
        assert set(cols) == {
            "donor",
            "match_value",
            "match_score",
            "matched_row_id",
            *carry,
        }
        assert (
            sorted(p.get_values(child["id"], cols["matched_row_id"]["id"]).values())
            == b_rows[:2]
        )
        if carry:
            assert cols["location"]["type"] == "text"
            values = p.get_values(child["id"], cols["location"]["id"])
            assert {row["parent_row_id"]: values[row["id"]] for row in rows} == {
                a_rows[0]: "Boston",
                a_rows[1]: "NYC",
            }
        run = p.db.execute(
            "SELECT completed_rows,failed_rows FROM runs WHERE id=?", (result.run_id,)
        ).fetchone()
        assert tuple(run) == (3, 0)
    finally:
        p.close()


def test_child_sheet_name_collision_rejected(tmp_path):
    p, a, b, *_ = _project(tmp_path)
    try:
        result = run_action_spec(
            p, _request(a, b, sheet_name="registry"), project_id="test"
        )
        assert result.status == "failed"
        assert len(p.sheets()) == 2
        assert p.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        p.close()


def test_semantic_join_endpoint_materializes_child_sheet(tmp_path):
    # join.semantic is queued placement (join-semantic-queued-conversion-v1):
    # launch returns the run handle immediately; the derive child sheet
    # materializes on the worker, after drain_queue().
    client = _client(tmp_path)
    pid, donors, registry = _api_project(client)

    key = "semantic-join-materialize@sha256:stable"
    gated = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_request(
            donors,
            registry,
            output_names={
                "source": "donor",
                "match_value": "match_value",
                "match_score": "match_score",
                "matched_row_id": "match_row_id",
            },
            sheet_name="linkage",
            idempotency_key=key,
        ),
    )
    assert gated.status_code == 402, gated.text
    promise_set_hash = gated.json()["errors"][0]["details"]["promise_set_hash"]

    r = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={
            **_request(
                donors,
                registry,
                output_names={
                    "source": "donor",
                    "match_value": "match_value",
                    "match_score": "match_score",
                    "matched_row_id": "match_row_id",
                },
                sheet_name="linkage",
                idempotency_key=key,
            ),
            "confirmation": promise_set_hash,
        },
    )

    assert r.status_code == 200, r.text
    out = r.json()
    assert out["schema_version"] == "frisket.action_result.v1"
    assert out["status"] == "queued"
    assert out["action"]["kind"] == "join.semantic"
    assert out["run_id"] is not None
    assert out["job_id"] is not None
    receipt_id = out["receipt_id"]
    assert receipt_id

    drain_queue(client)

    project = client.app.state.workspace.get(pid)
    completed_receipt = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?",
            (receipt_id,),
        ).fetchone()["body"]
    )
    assert completed_receipt["status"] == "completed", [
        error["message"] for error in completed_receipt["errors"]
    ]
    sheet_row = project.db.execute(
        "SELECT id FROM sheets WHERE name=?", ("linkage",)
    ).fetchone()
    assert sheet_row is not None
    sheet_id = int(sheet_row["id"])

    receipt_row = project.db.execute(
        "SELECT status FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["status"] == "completed"

    data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    assert data["total"] == 2
    cols = {c["name"]: c for c in data["columns"]}
    assert set(cols) == {"donor", "match_value", "match_score", "match_row_id"}
    assert cols["match_score"]["type"] == "number"


def _api_project_with_location(client: TestClient) -> tuple[str, int, int]:
    pid = client.post("/api/projects", json={"name": "Join API Carry"}).json()["id"]
    donors = client.post(
        f"/api/projects/{pid}/import/csv",
        files={
            "file": (
                "donors.csv",
                "donor,location\nACME Corp,Boston\nGlobex,NYC\nUmbrella Holdings,LA\n",
                "text/csv",
            )
        },
    )
    registry = client.post(
        f"/api/projects/{pid}/import/csv",
        files={
            "file": (
                "registry.csv",
                "company\nAcme Corporation\nGlobex LLC\nInitech Inc\n",
                "text/csv",
            )
        },
    )
    assert donors.status_code == 200, donors.text
    assert registry.status_code == 200, registry.text
    return pid, donors.json()["sheet_id"], registry.json()["sheet_id"]


def test_semantic_join_endpoint_carries_columns_into_child_sheet(tmp_path):
    # join.semantic is queued placement (join-semantic-queued-conversion-v1):
    # the child sheet only exists after drain_queue().
    client = _client(tmp_path)
    pid, donors, registry = _api_project_with_location(client)

    key = "semantic-join-carry-columns@sha256:stable"
    gated = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_request(
            donors,
            registry,
            output_names={
                "source": "donor",
                "carry.location": "location",
                "match_value": "match_value",
                "match_score": "match_score",
                "matched_row_id": "match_row_id",
            },
            sheet_name="linkage",
            carry=["location"],
            idempotency_key=key,
        ),
    )
    assert gated.status_code == 402, gated.text
    promise_set_hash = gated.json()["errors"][0]["details"]["promise_set_hash"]

    r = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={
            **_request(
                donors,
                registry,
                output_names={
                    "source": "donor",
                    "carry.location": "location",
                    "match_value": "match_value",
                    "match_score": "match_score",
                    "matched_row_id": "match_row_id",
                },
                sheet_name="linkage",
                carry=["location"],
                idempotency_key=key,
            ),
            "confirmation": promise_set_hash,
        },
    )

    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "queued"

    drain_queue(client)

    project = client.app.state.workspace.get(pid)
    sheet_row = project.db.execute(
        "SELECT id FROM sheets WHERE name=?", ("linkage",)
    ).fetchone()
    assert sheet_row is not None
    sheet_id = int(sheet_row["id"])

    data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    assert data["total"] == 2
    cols = {c["name"]: c for c in data["columns"]}
    assert set(cols) == {
        "donor",
        "location",
        "match_value",
        "match_score",
        "match_row_id",
    }
    assert cols["location"]["type"] == "text"


@pytest.mark.parametrize(
    ("carry_columns", "error_code"),
    [(["not_a_column"], "invalid_input_ref"), (["donor"], "invalid_params")],
)
def test_semantic_join_endpoint_rejects_bad_carry_columns(
    tmp_path, carry_columns, error_code
):
    client = _client(tmp_path)
    pid, donors, registry = _api_project(client)

    r = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_request(donors, registry, sheet_name="linkage", carry=carry_columns),
    )

    assert r.status_code == 400
    body = r.json()
    assert body["status"] == "failed"
    assert body["errors"][0]["code"] == error_code


def test_semantic_join_endpoint_maps_user_errors_to_400(tmp_path):
    client = _client(tmp_path)
    pid, donors, _registry = _api_project(client)

    r = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_request(
            donors, donors, target_column="donor", sheet_name="same-sheet-linkage"
        ),
    )

    assert r.status_code == 400
    body = r.json()
    assert body["schema_version"] == "frisket.action_result.v1"
    assert body["status"] == "failed"
    assert body["errors"][0]["code"] == "invalid_params"
    assert (
        "different target sheet" in body["errors"][0]["details"]["errors"][0]["message"]
    )


def test_semantic_join_endpoint_maps_embedding_failure_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder", lambda router=None, **_kw: None
    )
    client = _client(tmp_path)
    pid, donors, registry = _api_project(client)

    r = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_request(donors, registry, sheet_name="linkage"),
    )

    assert r.status_code == 400
    body = r.json()
    assert body["schema_version"] == "frisket.action_result.v1"
    assert body["status"] == "failed"
    assert body["errors"][0]["code"] == "invalid_params"
    assert "embedding backend" in body["errors"][0]["details"]["errors"][0]["message"]


def test_semantic_join_remote_embedder_gates_before_any_embed_call(
    tmp_path,
    monkeypatch,
):
    """The REAL cost-gate fence (closure-sweep audit C4). The old test here
    monkeypatched MapRunner.prepare_run to raise CostGate itself, which
    proved only the error mapping — under it, join.semantic shipped with a
    hard-coded $0 estimate that let the remote-OpenAI embedding fallback
    spend real money with `confirmed` as dead code.

    This test uses the real gate machinery end to end: local embeddings
    disabled (FRISKET_DISABLE_LOCAL_EMBED=1, the resolve_embedder escape
    hatch), a router that genuinely reports a remote embedding backend and
    RECORDS every embed call. confirmed=false must yield the 402
    needs_confirmation envelope with ZERO embed calls and no run/job — the
    remote spend is gated BEFORE any egress."""
    from frisket import semantic as semantic_module
    from frisket.server.app import create_app

    # undo this module's autouse resolve_embedder stub: the fence needs the
    # real resolution (local disabled -> remote fallback), not a fake backend
    monkeypatch.setattr("frisket.semantic.resolve_embedder", _REAL_RESOLVE_EMBEDDER)
    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    monkeypatch.setenv("FRISKET_ENABLE_PROVIDERLESS_CLASSIFY", "1")
    monkeypatch.setenv("FRISKET_PROVIDERLESS_CLASSIFY_THREADS", "2")
    assert semantic_module.local_embedder() is None

    embed_calls = _patch_async_remote_embedding(monkeypatch)
    router = ModelRouter(
        keys={"openai": "sk-test"}, cache=None, cache_mode="off", use_env_keys=False
    )
    assert router.has_embedding_backend()
    client = TestClient(create_app(tmp_path / "workspace", router=router))
    pid, donors, registry = _api_project(client)

    r = client.post(
        f"/api/projects/{pid}/actions/v1/run", json=_request(donors, registry)
    )

    # HARD CONSTRAINT: the cost-confirmation gate fires at REQUEST time --
    # no run, no job, and ZERO embedding calls reached the provider.
    assert r.status_code == 402, r.text
    body = r.json()
    assert body["schema_version"] == "frisket.action_result.v1"
    assert body["status"] == "needs_confirmation"
    assert body["run_id"] is None
    assert body["job_id"] is None
    assert body["errors"][0]["code"] == "model_cost_requires_confirmation"
    estimate = body["errors"][0]["details"]["estimate"]
    assert estimate["cost"] is None
    assert estimate["cost_source"] == "unknown"
    assert "pricing_key" not in estimate
    assert embed_calls == []

    project = client.app.state.workspace.get(pid)
    run_count = project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert run_count == 0


def test_semantic_join_tiny_known_zero_quote_is_preapproved(tmp_path, monkeypatch):
    """A known billed $0.0 quote is at-or-below the standing threshold.

    Unknown-cost remote embeddings remain gated by the preceding test. This
    case exercises a priced remote model whose exact quote is below half a
    micro-dollar, so the canonical billed amount is $0.0 and can queue.
    """
    from frisket import semantic as semantic_module
    from frisket.server.app import create_app

    monkeypatch.setattr("frisket.semantic.resolve_embedder", _REAL_RESOLVE_EMBEDDER)
    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    assert semantic_module.local_embedder() is None
    # Price the remote embed model so cheap the 6-decimal rounding lands on
    # exactly 0.0 for this corpus ($0.001/MTok input).
    from frisket.ai.llm import ModelPricing

    monkeypatch.setattr(
        "frisket.ai.llm.model_pricing",
        lambda model_id: ModelPricing(
            price=(0.001, 0.0),
            cost_source="pricing_data",
            pricing_key=f"{model_id}.tokens",
        ),
    )

    embed_calls = _patch_async_remote_embedding(monkeypatch)
    router = ModelRouter(
        keys={"openai": "sk-test"}, cache=None, cache_mode="off", use_env_keys=False
    )
    client = TestClient(create_app(tmp_path / "workspace", router=router))
    pid, donors, registry = _api_project(client)

    estimate_response = client.post(
        f"/api/projects/{pid}/actions/v1/estimate",
        json={"action": _request(donors, registry)},
    )
    assert estimate_response.status_code == 200, estimate_response.text
    estimate = estimate_response.json()["estimate"]
    assert estimate["cost"] == 0.0
    assert estimate["cost_source"] == "pricing_data"
    assert estimate["pricing_key"] == f"{semantic_module.REMOTE_EMBED_MODEL}.tokens"
    assert embed_calls == []

    response = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_request(donors, registry),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["run_id"] is not None and body["job_id"] is not None
    assert embed_calls == []


def _patch_async_remote_embedding(monkeypatch):
    """Patch the router at the real async interface used by the remote bridge."""

    from frisket.ai.embeddings import build_batch_result

    calls: list[list[str]] = []

    async def recording_embed_batch(self, texts, **kwargs):
        del kwargs
        batch = list(texts)
        calls.append(batch)
        return build_batch_result(
            [VECS[text] for text in batch],
            provider_id="openai",
            provider_kind="platform_api",
            requested_model="text-embedding-3-small",
            actual_model_id="text-embedding-3-small",
            credential_source=self.credential_source_for("openai"),
            usage={"input_count": len(batch)},
            cost_source="unknown",
        )

    monkeypatch.setattr(ModelRouter, "embed_batch", recording_embed_batch)
    return calls


def _patch_paid_remote_result(monkeypatch):
    """Let the join complete while preserving what the paid provider returned.

    This shim returns the complete neutral provider result through the new
    dual-shape seam while retaining each batch for the money-path assertions.
    """
    from frisket.ai.embeddings import build_batch_result
    from frisket.semantic import REMOTE_EMBED_MODEL

    paid_results = []

    def resolve_paid_remote(router=None, *, allow_remote=False):
        # estimate() probes without a router and must still classify this as
        # the remote fallback, so the exact 402/echo path stays real.
        if router is None or not allow_remote or not router.has_embedding_backend():
            return None

        def embed(texts):
            batch = list(texts)
            result = build_batch_result(
                [VECS[text] for text in batch],
                provider_id="openai",
                provider_kind="platform_api",
                requested_model="text-embedding-3-small",
                actual_model_id="text-embedding-3-small",
                credential_source=router.credential_source_for("openai"),
                usage={"input_count": len(batch)},
                provider_request_id=f"req-{len(paid_results) + 1}",
                provider_reported_cost_usd=0.01,
                provider_cost_usd=0.01,
                cost_source="provider_reported",
            )
            paid_results.append(result)
            return result

        return embed, REMOTE_EMBED_MODEL

    monkeypatch.setattr("frisket.semantic.resolve_embedder", resolve_paid_remote)
    return paid_results


def _post_bound_remote_join(
    client,
    pid,
    donors,
    registry,
    *,
    idempotency_key,
    output_name="remote_match",
    child_sheet_name="Remote Links",
):
    gate = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_request(
            donors,
            registry,
            output_names={
                "match_value": f"{output_name}_value",
                "match_score": f"{output_name}_score",
                "matched_row_id": f"{output_name}_row_id",
            },
            sheet_name=child_sheet_name,
            idempotency_key=idempotency_key,
        ),
    )
    assert gate.status_code == 402, gate.text
    context_hash = gate.json()["errors"][0]["details"]["promise_set_hash"]
    return client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={
            **_request(
                donors,
                registry,
                output_names={
                    "match_value": f"{output_name}_value",
                    "match_score": f"{output_name}_score",
                    "matched_row_id": f"{output_name}_row_id",
                },
                sheet_name=child_sheet_name,
                idempotency_key=idempotency_key,
            ),
            "confirmation": context_hash,
        },
    )


def test_remote_semantic_join_refuses_exhausted_project_key_before_queue_or_embed(
    tmp_path, monkeypatch
):
    """The remote-embedding confirmation covers egress; it does not waive the
    project key's budget.  Lowering the key to an exhausted cap between the
    402 and its bound retry must refuse before a job or provider call exists."""
    from frisket.server.app import create_app
    from frisket.team.security.secrets import encrypt_secret, key_hint

    monkeypatch.setattr("frisket.semantic.resolve_embedder", _REAL_RESOLVE_EMBEDDER)
    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    embed_calls = _patch_async_remote_embedding(monkeypatch)
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            router=ModelRouter(cache=None, cache_mode="off", use_env_keys=False),
        )
    )
    pid, donors, registry = _api_project(client)
    project = client.app.state.workspace.get(pid)
    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret("sk-project-openai"),
        hint=key_hint("sk-project-openai"),
        spend_cap_micro=None,
    )

    idempotency_key = "join.semantic@sha256:exhausted-project-key"
    gate = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_request(
            donors,
            registry,
            output_names={
                "match_value": "blocked_match_value",
                "match_score": "blocked_match_score",
                "matched_row_id": "blocked_match_row_id",
            },
            sheet_name="Blocked Links",
            idempotency_key=idempotency_key,
        ),
    )
    assert gate.status_code == 402, gate.text
    context_hash = gate.json()["errors"][0]["details"]["promise_set_hash"]
    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret("sk-project-openai"),
        hint=key_hint("sk-project-openai"),
        spend_cap_micro=0,
    )

    retry = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json={
            **_request(
                donors,
                registry,
                output_names={
                    "match_value": "blocked_match_value",
                    "match_score": "blocked_match_score",
                    "matched_row_id": "blocked_match_row_id",
                },
                sheet_name="Blocked Links",
                idempotency_key=idempotency_key,
            ),
            "confirmation": context_hash,
        },
    )
    assert retry.status_code == 400, retry.text
    body = retry.json()
    assert body["status"] == "failed"
    assert body["errors"][0]["code"] == "provider_spend_cap_exceeded"
    assert body["run_id"] is None
    assert body["job_id"] is None
    assert embed_calls == []


def test_remote_semantic_join_completes_through_the_async_router_bridge(
    tmp_path, monkeypatch
):
    """The real remote resolver must be operable from queued row execution.

    The metadata-rich async embedding bridge must complete inside the worker
    event loop and preserve unknown provider cost in durable facts and receipt.
    """
    from frisket.server.app import create_app
    from frisket.team.security.secrets import encrypt_secret, key_hint

    monkeypatch.setattr("frisket.semantic.resolve_embedder", _REAL_RESOLVE_EMBEDDER)
    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    embed_calls = _patch_async_remote_embedding(monkeypatch)
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            router=ModelRouter(cache=None, cache_mode="off", use_env_keys=False),
        )
    )
    pid, donors, registry = _api_project(client)
    project = client.app.state.workspace.get(pid)
    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret("sk-project-openai"),
        hint=key_hint("sk-project-openai"),
        spend_cap_micro=1_000_000,
    )

    launch = _post_bound_remote_join(
        client,
        pid,
        donors,
        registry,
        idempotency_key="join.semantic@sha256:remote-async-bridge",
        output_name="bridge_match",
        child_sheet_name="Bridge Links",
    )
    assert launch.status_code == 200, launch.text
    launched = launch.json()
    drain_queue(client)

    status = client.get(
        f"/api/projects/{pid}/actions/runs/{launched['run_id']}/status"
    ).json()["run"]["public_status"]
    assert (
        status["status"],
        status["completed"],
        status["failed"],
        bool(embed_calls),
    ) == ("completed", status["total"], 0, True), status
    model_calls = project.db.execute(
        "SELECT provider_cost_usd FROM model_calls WHERE run_id=?",
        (launched["run_id"],),
    ).fetchall()
    spend = project.provider_spend_state("openai")
    receipt = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (launched["receipt_id"],)
        ).fetchone()["body"]
    )
    assert len(model_calls) == len(embed_calls)
    assert all(call["provider_cost_usd"] is None for call in model_calls)
    assert spend is not None
    assert (spend.spent_micro, spend.unmetered_calls) == (0, len(embed_calls))
    assert [
        {
            "provider": item["provider"],
            "external_api": item["external_api"],
            "model_call_count": item["model_call_count"],
            "cost_actual": item["cost_actual"],
        }
        for item in receipt["provider_use"]
    ] == [
        {
            "provider": "openai",
            "external_api": True,
            "model_call_count": len(embed_calls),
            "cost_actual": None,
        }
    ]


def test_remote_semantic_join_persists_each_call_and_receipts_actual_cost(
    tmp_path, monkeypatch
):
    """Every paid vector request must survive as one model_call, accrue the
    project key once, and roll up to the join receipt.  The prior vector-only
    seam discarded all three facts and then printed a confident remote $0."""
    import json

    from frisket.server.app import create_app
    from frisket.team.security.secrets import encrypt_secret, key_hint

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    paid_results = _patch_paid_remote_result(monkeypatch)
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            router=ModelRouter(cache=None, cache_mode="off", use_env_keys=False),
        )
    )
    pid, donors, registry = _api_project(client)
    project = client.app.state.workspace.get(pid)
    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret("sk-project-openai"),
        hint=key_hint("sk-project-openai"),
        spend_cap_micro=1_000_000,
    )

    launch = _post_bound_remote_join(
        client,
        pid,
        donors,
        registry,
        idempotency_key="join.semantic@sha256:remote-provider-facts",
    )
    assert launch.status_code == 200, launch.text
    launched = launch.json()
    assert launched["status"] == "queued"
    drain_queue(client)
    assert paid_results

    status = client.get(
        f"/api/projects/{pid}/actions/runs/{launched['run_id']}/status"
    ).json()["run"]["public_status"]
    assert status["status"] == "completed", status

    model_calls = project.db.execute(
        "SELECT * FROM model_calls WHERE run_id=? ORDER BY created_at, id",
        (launched["run_id"],),
    ).fetchall()
    expected_cost = len(paid_results) * 0.01
    spend = project.provider_spend_state("openai")
    assert spend is not None

    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (launched["receipt_id"],)
    ).fetchone()
    receipt = json.loads(receipt_row["body"])
    provider_use = receipt["provider_use"]
    receipt_money = [
        {
            "provider": item.get("provider"),
            "credential_source": item.get("credential_source"),
            "external_api": item.get("external_api"),
            "model_call_count": item.get("model_call_count"),
            "cost_actual": item.get("cost_actual"),
        }
        for item in provider_use
    ]
    observed = {
        "model_call_count": len(model_calls),
        "model_call_capabilities": {call["capability"] for call in model_calls},
        "model_call_providers": {call["provider"] for call in model_calls},
        "model_call_credential_sources": {
            call["credential_source"] for call in model_calls
        },
        "model_call_costs": {call["provider_cost_usd"] for call in model_calls},
        "spent_micro": spend.spent_micro,
        "unmetered_calls": spend.unmetered_calls,
        "receipt_provider_use": receipt_money,
    }
    expected = {
        "model_call_count": len(paid_results),
        "model_call_capabilities": {"llm.embed"},
        "model_call_providers": {"openai"},
        "model_call_credential_sources": {"project_key"},
        "model_call_costs": {0.01},
        "spent_micro": round(expected_cost * 1_000_000),
        "unmetered_calls": 0,
        "receipt_provider_use": [
            {
                "provider": "openai",
                "credential_source": "project_key",
                "external_api": True,
                "model_call_count": len(paid_results),
                "cost_actual": expected_cost,
            }
        ],
    }
    assert observed == expected


def test_remote_semantic_join_retry_refuses_to_rebuy_fact_without_cached_vectors(
    tmp_path, monkeypatch
):
    """A provider success can commit its fact just before the sidecar fails.

    Replaying the exact action must recognize the durable batch identity and
    refuse. An explicitly authorized backfill is a fresh successor, so it may
    buy again under fresh run-scoped call identities and must account for that
    spend truthfully.
    """
    from frisket import semantic as semantic_module
    from frisket.team.security.secrets import encrypt_secret, key_hint

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    paid_results = _patch_paid_remote_result(monkeypatch)
    project, source_sheet, target_sheet, source_rows, _target_rows = _project(tmp_path)
    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret("sk-project-openai"),
        hint=key_hint("sk-project-openai"),
        spend_cap_micro=1_000_000,
    )
    router = ModelRouter(
        keys={"openai": "sk-project-openai"},
        key_sources={"openai": "project_key"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
    )
    real_sidecar = semantic_module._sidecar
    failed_once = False

    class FailAfterFactBeforeVectors:
        def __init__(self, db):
            self._db = db

        def __getattr__(self, name):
            return getattr(self._db, name)

        def executemany(self, statement, rows):
            del statement, rows
            assert (
                project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
                == 1
            )
            raise RuntimeError("injected vector sidecar commit failure")

    def fail_first_sidecar_write(owner):
        nonlocal failed_once
        db = real_sidecar(owner)
        if failed_once:
            return db
        failed_once = True
        return FailAfterFactBeforeVectors(db)

    monkeypatch.setattr(semantic_module, "_sidecar", fail_first_sidecar_write)
    request = _request(
        source_sheet,
        target_sheet,
        sheet_name="Retry Safety Links",
        idempotency_key="join.semantic@sha256:checkpoint-retry-first",
    )
    first = _run_confirmed(project, router, request)
    assert first.run_id is not None
    replay = run_action_spec(
        project,
        request,
        project_id="semantic-join-unit",
        router=router,
    )
    assert replay.status == first.status
    assert replay.run_id == first.run_id
    assert replay.receipt_id == first.receipt_id
    assert [error.code for error in replay.errors] == [
        error.code for error in first.errors
    ]
    assert len(paid_results) == 1
    backfill_action = {
        "action_id": "run.backfill",
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": source_sheet,
            "row_ids": source_rows,
        },
        "params": {"column": "match_value"},
        "idempotency_key": "run.backfill@sha256:semantic-checkpoint-retry",
    }
    resumed = run_action_spec(
        project,
        backfill_action,
        project_id="semantic-join-unit",
        router=router,
    )
    if resumed.status == "needs_confirmation":
        promise_set_hash = resumed.errors[0].details.get("promise_set_hash")
        assert isinstance(promise_set_hash, str) and promise_set_hash
        resumed = run_action_spec(
            project,
            {
                **backfill_action,
                "confirmation": promise_set_hash,
            },
            project_id="semantic-join-unit",
            router=router,
        )
    source_calls = project.db.execute(
        "SELECT id, provider_cost_usd FROM model_calls WHERE run_id=?",
        (first.run_id,),
    ).fetchall()
    spend = project.provider_spend_state("openai")
    errors = [
        row["error"]
        for row in project.db.execute(
            "SELECT DISTINCT error FROM results WHERE run_id=? ORDER BY error",
            (first.run_id,),
        ).fetchall()
        if row["error"]
    ]
    # Recovery is a fresh scoped successor; the failed generation is never
    # reopened. Paid-batch identities are run-scoped, so the confirmed fresh
    # run records its own calls rather than inheriting the source run's facts.
    assert resumed.run_id != first.run_id
    expected_successor_calls = 1 + len(source_rows)
    assert len(paid_results) == 1 + expected_successor_calls
    assert [(row["provider_cost_usd"]) for row in source_calls] == [0.01]
    successor_calls = project.db.execute(
        "SELECT provider_cost_usd FROM model_calls WHERE run_id=? ORDER BY id",
        (resumed.run_id,),
    ).fetchall()
    assert [row["provider_cost_usd"] for row in successor_calls] == [
        0.01
    ] * expected_successor_calls
    assert spend is not None
    assert (spend.spent_micro, spend.unmetered_calls) == (
        10_000 * (1 + expected_successor_calls),
        0,
    )
    assert errors
    assert any("refusing to call the provider again" in error for error in errors)


def test_direct_join_semantic_stale_clear_retry_resumes_abandoned_run(
    tmp_path, monkeypatch
):
    """Defect 1 regression: a hard process kill (SIGKILL, OOM, host reboot)
    leaves the action receipt stuck 'running' forever -- nothing runs to
    finalize it, because nothing runs at all. Once the 1-hour stale-clear
    window passes, a retry with the SAME idempotency_key must resume the
    abandoned run's row checkpoints -- not mint a fresh run_id that cannot
    see rows already bought under the abandoned run and re-buys them.

    A raised Python exception is not a faithful crash: every escape hatch
    in this codebase (NetworkDisabled, ValueError/RuntimeError, even a bare
    ``except Exception`` and, empirically, an in-loop ``KeyboardInterrupt``
    swallowed by asyncio's own cancellation bookkeeping) still runs enough
    cleanup to terminalize the run as 'cancelled' rather than leave it
    'running'. The only faithful simulation is an actual hard process exit
    (``os._exit`` in a forked child) after the first row's paid checkpoint
    has committed: no unwind, no `finally`, no cancellation bookkeeping --
    exactly what a real crash leaves behind.
    """
    from datetime import datetime, timedelta, timezone

    from frisket.engine.executor.action_reservations import (
        RUNNING_RECEIPT_STALE_AFTER_SECONDS,
    )
    from frisket.engine.store import Project
    from frisket.engine.store.runs import RunResultStore
    from frisket.execution.attempt import abandon_stale_dispatching_attempts
    from frisket.team.security.secrets import encrypt_secret, key_hint

    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    _patch_paid_remote_result(monkeypatch)

    bundle_path = tmp_path / "crash.frisket"
    project = Project.create(bundle_path, name="t")
    source_sheet = project.add_sheet("donors")
    donor_cols = {"donor": project.add_column(source_sheet, "donor", type="text")}
    source_rows = project.add_rows(
        source_sheet,
        [
            {"donor": "ACME Corp"},
            {"donor": "Globex"},
            {"donor": "Umbrella Holdings"},
        ],
        donor_cols,
    )
    target_sheet = project.add_sheet("registry")
    registry_cols = {
        "company": project.add_column(target_sheet, "company", type="text")
    }
    project.add_rows(
        target_sheet,
        [
            {"company": "Acme Corporation"},
            {"company": "Globex LLC"},
            {"company": "Initech Inc"},
        ],
        registry_cols,
    )
    project.set_provider_key(
        provider="openai",
        encrypted=encrypt_secret("sk-project-openai"),
        hint=key_hint("sk-project-openai"),
        spend_cap_micro=1_000_000,
    )
    project.close()

    idempotency_key = "join.semantic@sha256:stale-clear-resume"
    request = _request(
        source_sheet,
        target_sheet,
        sheet_name="Stale Retry Links",
        idempotency_key=idempotency_key,
    )

    def dispatch(project_handle, router_handle):
        gated = run_action_spec(
            project_handle,
            request,
            project_id="semantic-join-unit",
            router=router_handle,
        )
        if gated.status != "needs_confirmation":
            return gated
        return run_action_spec(
            project_handle,
            {**request, "confirmation": gated.errors[0].details["promise_set_hash"]},
            project_id="semantic-join-unit",
            router=router_handle,
        )

    def make_router():
        return ModelRouter(
            keys={"openai": "sk-project-openai"},
            key_sources={"openai": "project_key"},
            cache=None,
            cache_mode="off",
            use_env_keys=False,
        )

    pid = os.fork()
    if pid == 0:
        # Child: reopen a fresh connection and hard-exit the moment the
        # first row's checkpoint commits -- simulating the crash with no
        # Python unwind of any kind.
        child_project = Project(bundle_path)
        real_complete = RunResultStore.complete_row_effect_checkpoint
        completed = 0

        def crash_after_first_row(self, *args, **kwargs):
            nonlocal completed
            result = real_complete(self, *args, **kwargs)
            completed += 1
            if completed == 1:
                os._exit(1)
            return result

        RunResultStore.complete_row_effect_checkpoint = crash_after_first_row
        dispatch(child_project, make_router())
        os._exit(2)  # pragma: no cover - the crash above must fire first

    _, wait_status = os.waitpid(pid, 0)
    assert os.WIFEXITED(wait_status) and os.WEXITSTATUS(wait_status) == 1, (
        "the child must die by the injected os._exit(1), not run to completion"
    )

    project = Project(bundle_path)
    router = make_router()

    crashed_receipt = project.db.execute(
        "SELECT id, run_id, status FROM receipts WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    receipt_id = crashed_receipt["id"]
    run_a = int(crashed_receipt["run_id"])
    assert crashed_receipt["status"] == "running"
    assert (
        project.db.execute("SELECT status FROM runs WHERE id=?", (run_a,)).fetchone()[
            "status"
        ]
        == "running"
    )
    checkpoints_before = project.db.execute(
        "SELECT unit_key, state FROM effect_checkpoints "
        "WHERE family='row_effect' AND group_key=?",
        (str(run_a),),
    ).fetchall()
    assert [dict(row) for row in checkpoints_before] == [
        {"unit_key": "1", "state": "returned"}
    ]
    calls_before = project.db.execute(
        "SELECT COUNT(*) FROM model_calls WHERE run_id=?", (run_a,)
    ).fetchone()[0]
    assert calls_before > 0

    # Age the receipt past its 1-hour stale-clear window
    # (RUNNING_RECEIPT_STALE_AFTER_SECONDS).
    project.db.execute(
        "UPDATE receipts SET created_at=datetime('now', ?) WHERE id=?",
        (f"-{RUNNING_RECEIPT_STALE_AFTER_SECONDS + 60} seconds", receipt_id),
    )
    project.db.commit()

    # A crashed 'dispatching' attempt and its output-column claims use a
    # separate, already-modeled recovery gate from the
    # receipt's own stale-clear: every AttemptAuthority.mint() already runs
    # abandon_stale_dispatching_attempts before admitting a new attempt
    # (execution/attempt_authority.py:152), but only once BOTH the attempt
    # and its claims' 6-hour lease (STALE_DISPATCHING_AGE) are old enough --
    # a real crash discovered hours later clears this organically as a
    # byproduct of the next dispatch. Simulate "hours passed" the same way
    # this call already gets exercised in production (a `now` far enough
    # ahead), and release the claims themselves (their `status` never
    # expires on its own -- OutputColumnClaimStore marks this exact shape
    # `requires_recovery: True` for an operator/reconcile flow, defect 4's
    # territory) to isolate what THIS defect's fix owns: once the crashed
    # attempt is closed, does the retry resume run_a's row checkpoints
    # instead of re-buying them?
    project.db.execute(
        "UPDATE output_column_claims SET status='released', "
        "released_at=datetime('now') WHERE run_id=? AND status='active'",
        (run_a,),
    )
    project.db.commit()
    abandoned = abandon_stale_dispatching_attempts(
        project,
        run_a,
        now=datetime.now(timezone.utc) + timedelta(hours=7),
    )
    assert abandoned == 1

    # Attempt 1: finds the stale 'running' receipt, clears it, asks for a
    # retry -- the same two-step stale-clear contract every reserved-
    # maprunner action shares (_running_receipt_stale_result).
    cleared = dispatch(project, router)
    assert cleared.status == "failed"
    assert [error.code for error in cleared.errors] == ["idempotency_stale_running"]
    assert (
        project.db.execute(
            "SELECT 1 FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()
        is None
    )

    row_one_calls_before = project.db.execute(
        "SELECT COUNT(*) FROM model_calls WHERE run_id=? AND row_id=?",
        (run_a, source_rows[0]),
    ).fetchone()[0]
    assert row_one_calls_before > 0

    # Clearing the receipt does not erase the request identity persisted with
    # the abandoned run. A changed request cannot borrow its paid checkpoints.
    for changed in (
        {**request, "params": {**request["params"], "match_threshold": 0.75}},
        {**request, "sheet_name": "Different result"},
        {**request, "scope": {**request["scope"], "row_ids": source_rows[:1]}},
    ):
        refused = run_action_spec(
            project,
            changed,
            project_id="semantic-join-unit",
            router=router,
        )
        assert refused.status == "failed"
        assert refused.run_id is None
        assert refused.errors[0].code == "invalid_params"
        assert "abandoned semantic join differs" in str(refused.errors[0].details)
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert (
            project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
            == calls_before
        )

    # Attempt 2 (the actual retry): must RESUME run_a -- not mint a fresh
    # run -- and row 1's already-paid checkpoint must be honored, not
    # re-bought, while rows 2 and 3 (never attempted before the crash) are
    # bought for the first time.
    resumed = dispatch(project, router)
    assert resumed.status == "completed", resumed.errors
    assert resumed.run_id == run_a

    row_one_calls_after = project.db.execute(
        "SELECT COUNT(*) FROM model_calls WHERE run_id=? AND row_id=?",
        (run_a, source_rows[0]),
    ).fetchone()[0]
    assert row_one_calls_after == row_one_calls_before  # never re-bought
    for later_row_id in source_rows[1:]:
        later_calls = project.db.execute(
            "SELECT COUNT(*) FROM model_calls WHERE run_id=? AND row_id=?",
            (run_a, later_row_id),
        ).fetchone()[0]
        assert later_calls == 1  # bought for the first time on resume
    total_calls_after = project.db.execute(
        "SELECT COUNT(*) FROM model_calls WHERE run_id=?", (run_a,)
    ).fetchone()[0]
    assert total_calls_after > calls_before


def test_semantic_join_worker_refuses_undeclared_remote_fallback(tmp_path, monkeypatch):
    """A local enqueue decision cannot silently become remote in the worker."""
    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder",
        lambda router=None, **_kw: (_fake_embed, "fastembed/test-local"),
    )
    client = _client(tmp_path)
    pid, source, target = _api_project(client)
    launch = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_request(source, target),
    )
    assert launch.status_code == 200, launch.text
    launched = launch.json()
    assert launched["status"] == "queued"
    remote_calls = []

    def remote_embed(texts):
        remote_calls.append(list(texts))
        return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder",
        lambda router=None, **_kw: (remote_embed, "openai/text-embedding-3-small"),
    )
    drain_queue(client)
    project = client.app.state.workspace.get(pid)
    receipt = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?",
            (launched["receipt_id"],),
        ).fetchone()["body"]
    )
    assert receipt["status"] == "failed"
    assert remote_calls == []
    assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
    assert not any(sheet["name"] == "Matches" for sheet in project.sheets())
