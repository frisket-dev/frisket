"""First-party action placement is explicit execution metadata.

CI requires every v1 kind to occupy exactly one declared placement bucket;
there is no production fallback that chooses a safe placement for a missing
declaration. Runtime-extensible recipe registrations are outside this closed
first-party execution-metadata inventory.

Scope:
  (a) contract tests require every first-party action kind to be declared in
      exactly one placement set
      (queued_project_run, queued_action_job, inline, or outlier).
  (b) the declared-inline set (_INLINE_KINDS) becomes explicit, one
      justification string per entry.
  (c) enrich.census_demographics, enrich.geocode, and map.python retain queued
      placement. These programs reconstruct their typed queue adapters from
      the canonical request, including the Python evaluator program.

DIVERGENCE: ``join.semantic`` cannot move with the three media-acquisition
kinds because ``semantic.py``'s ``_JOIN_SEMANTIC_RUN_FN`` is
built by `build_reserved_model_child_sheet_run_fn` (an `_ActionCoreSpec` with
`body_kind="child_sheet_model"` -- the reduce.group_summary/child-sheet-write
archetype), NOT `build_reserved_spec`'s `_ReservedMaprunnerActionSpec`
archetype the mechanical `_queued_action_spec_from_reserved` flip requires.
join.semantic stayed declared inline pending its own queue machinery, the
same class of exclusion as media.capture_url before its stage-1 redesign.
That follow-up (join-semantic-queued-conversion-v1) has since landed: a
hand-built `_QueuedActionSpec` for the child_sheet_model body kind (see the
design note in executor/action_families/semantic.py beside
`_queued_resolve_join_semantic`), so join.semantic now JOINS
_QUEUED_PROJECT_RUN_KINDS too --
`test_join_semantic_joined_queued_project_run_via_new_child_sheet_model_queue_machinery`
below retargets what was this file's exclusion pin.

Full request/worker round trips (the driving-example depth
media-acquisition-queued-placement-v1 set) are proven here for map.python
(the simplest of the three: no external credentials/network). Declared-
placement + registry-wiring + request-time-gate proofs (the "prove the
machinery, don't repeat a full worker round trip per kind" depth that test
file's docstring documents) cover enrich.geocode and
enrich.census_demographics -- their request-time gates (geocode's cost
confirmation, census's credential gate) are proven directly since those are
this task's explicit "stays green" pin.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.actions.registry import ACTION_REGISTRY
from frisket.contracts.action import (
    ActionResult,
)
from frisket.engine.executor.action_dispatch import placement_for_kind
from frisket.engine.executor.action_specs import (
    ACCEPTED_EXECUTION_OUTLIERS,
    PlacementPolicy,
    declared_inline_kinds,
    declared_queued_action_job_kinds,
    declared_queued_project_run_kinds,
    execution_spec_for,
)
from frisket.engine.executor.queue_policy import INTENTIONALLY_DIRECT_V1_ACTIONS
from frisket.engine.executor.queued_actions import (
    queued_v1_action_request,
)
from frisket.engine.jobs import HandlerRegistry, Worker, WorkerPorts
from frisket.engine.jobs.runs import register_project_run_handler
from frisket.execution.pricing_policy import (
    QuoteFacts,
    RatedQuote,
    install_pricing_policy,
)
from frisket.server.app import create_app
from http_test_helpers import drain_queue


MOVED_KINDS = ("enrich.census_demographics", "enrich.geocode", "map.python")


class _AboveLimitPolicy:
    """Make this request genuinely above the local preapproval threshold."""

    policy_id = "test.placement.geocode-above-limit.v1"

    def rate(self, facts: QuoteFacts) -> RatedQuote:
        return RatedQuote(
            billed_cost=2_000_001,
            provider_cost=facts.provider_cost,
            lane="cost_plus",
            policy_id=self.policy_id,
        )


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "ws", run_status_grace_seconds=3600.0))


# --- (a) raw placement-declaration completeness -----------------------------


def test_raw_placement_declarations_pairwise_partition_all_v1_kinds() -> None:
    buckets = {
        "inline": set(declared_inline_kinds()),
        "queued_project": set(declared_queued_project_run_kinds()),
        "queued_action": set(declared_queued_action_job_kinds()),
        "outlier": set(ACCEPTED_EXECUTION_OUTLIERS),
    }

    # Raw first-party declarations are pairwise disjoint; derived execution
    # registry views cannot mask overlap or omission in their source sets.
    entries = list(buckets.items())
    for index, (left_name, left) in enumerate(entries):
        for right_name, right in entries[index + 1 :]:
            assert left.isdisjoint(right), (left_name, right_name, left & right)

    # The same four raw buckets cover the first-party action domain exactly.
    covered = set().union(*buckets.values())
    assert covered == set(ACTION_REGISTRY.action_ids), {
        "uncovered": sorted(set(ACTION_REGISTRY.action_ids) - covered),
        "unknown": sorted(covered - set(ACTION_REGISTRY.action_ids)),
    }


def test_every_inline_entry_carries_a_real_one_line_justification() -> None:
    inline = declared_inline_kinds()
    assert len(inline) > 0
    for kind, justification in inline.items():
        assert isinstance(justification, str), kind
        assert len(justification) > 20, kind
        # Every declared-inline entry must place its lifecycle spec at INLINE.
        spec = execution_spec_for(kind)
        assert spec is not None, kind
        assert spec.lifecycle.placement is PlacementPolicy.INLINE, kind
        assert placement_for_kind(kind) is PlacementPolicy.INLINE, kind


# --- (c) the four moved kinds ------------------------------------------------


def test_moved_kinds_are_declared_queued_project_run_and_registry_wired() -> None:
    declared = declared_queued_project_run_kinds()
    for kind in MOVED_KINDS:
        assert kind in declared, kind
        spec = execution_spec_for(kind)
        assert spec is not None, kind
        assert spec.lifecycle.placement is PlacementPolicy.QUEUED_PROJECT_RUN, kind
        assert placement_for_kind(kind) is PlacementPolicy.QUEUED_PROJECT_RUN, kind
        queued = queued_v1_action_request(
            _python_action(1)
            if kind == "map.python"
            else {
                "action_id": kind,
                "scope": {"kind": "sheet_rows", "sheet_id": 1},
                "params": {"source": "source"},
                "idempotency_key": f"placement:{kind}",
            }
        )
        assert queued is not None
        assert queued.entry.kind == kind
        assert queued.program is not None
        assert queued.program.consumes_resolution is kind.startswith("enrich.")
        assert kind not in INTENTIONALLY_DIRECT_V1_ACTIONS, kind
        assert kind not in declared_inline_kinds(), kind


# --- enrich.geocode / enrich.census_demographics: launch-without-executing --
# proven via declared placement + registry wiring + the request-time gate,
# not a full worker round trip (media-acquisition-queued-placement-v1's
# established depth for non-driving-example kinds).


def _seed_geocode_project(client: TestClient) -> tuple[str, int]:
    project_id = client.post(
        "/api/projects", json={"name": "Placement queued geocode"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Places")
    address = project.add_column(sheet_id, "address", type="text")
    project.add_rows(
        sheet_id, [{"address": "1 Infinite Loop, Cupertino, CA"}], {"address": address}
    )
    return project_id, sheet_id


def test_enrich_geocode_cost_confirmation_gate_fires_before_any_run_is_created(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    install_pricing_policy(_AboveLimitPolicy())
    client = _client(tmp_path)
    project_id, sheet_id = _seed_geocode_project(client)
    body = {
        "action_id": "enrich.geocode",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": "address",
            "engine": "auto",
            "include_lat_lon": False,
        },
        "idempotency_key": "placement_default_queued_geocode@sha256:unconfirmed",
    }
    response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=body)
    # HARD CONSTRAINT: the 402 cost-confirmation gate still fires at REQUEST
    # time now that enrich.geocode is queued placement -- no run, no job.
    assert response.status_code == 402, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "needs_confirmation"
    assert result.run_id is None
    assert result.job_id is None
    assert result.errors[0].code == "external_cost_requires_confirmation"

    project = client.app.state.workspace.get(project_id)
    run_count = project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert run_count == 0
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    assert (
        project.db.execute("SELECT COUNT(*) FROM output_column_claims").fetchone()[0]
        == 0
    )
    assert (
        client.app.state.workspace.queue.list_project_jobs(
            project_id, kind="project.run"
        )
        == []
    )

    confirmed = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json={
            **body,
            "confirmation": result.errors[0].details["promise_set_hash"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    queued = ActionResult.model_validate(confirmed.json())
    assert queued.status == "queued"
    assert queued.run_id is not None
    assert queued.job_id is not None


def test_census_demographics_credential_gate_fires_before_any_run_is_created(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # action-api-key-gate-v1's ActionRunService.run_action choke point fires
    # BEFORE queue_v1_action_run branches on placement (server/services/
    # action_runs.py) -- this pins that it stays true now that
    # enrich.census_demographics is queued placement, not direct.
    monkeypatch.delenv("CENSUS_API_KEY", raising=False)
    client = _client(tmp_path)
    project_id = client.post(
        "/api/projects", json={"name": "Placement queued census"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Places")
    point = project.add_column(sheet_id, "point", type="geo_point")
    project.add_rows(
        sheet_id, [{"point": {"lat": 37.3318, "lon": -122.0312}}], {"point": point}
    )
    body = {
        "action_id": "enrich.census_demographics",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": "point",
            "geography": "tract",
            "include_moe": False,
        },
        "idempotency_key": "placement_default_queued_census@sha256:no-key",
    }
    response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=body)
    assert response.status_code == 400, response.text
    result = ActionResult.model_validate(response.json())
    assert result.status == "failed"
    assert result.run_id is None
    assert result.job_id is None
    assert result.errors[0].code == "missing_action_credential"

    run_count = project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert run_count == 0
    assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    assert (
        project.db.execute("SELECT COUNT(*) FROM output_column_claims").fetchone()[0]
        == 0
    )
    assert (
        client.app.state.workspace.queue.list_project_jobs(
            project_id, kind="project.run"
        )
        == []
    )


# --- map.python: full request/worker split, the driving example ------------


def _python_action(
    sheet_id: int, *, idempotency_key: str = "placement_default_queued_python@sha256:1"
) -> dict[str, Any]:
    return {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "input_columns": ["transcript"],
            "code": (
                "words = row['transcript'].split()\n"
                "result = {'excerpt': ' '.join(words[:3])}"
            ),
            "return_schema": {
                "type": "object",
                "properties": {"excerpt": {"type": "string"}},
                "required": ["excerpt"],
            },
            "output_routes": [
                {
                    "name": "excerpt",
                    "path": "$.excerpt",
                    "target": {"kind": "column", "type": "text"},
                }
            ],
        },
        "output_names": {"excerpt": "excerpt"},
        "idempotency_key": idempotency_key,
    }


def _seed_python_project(client: TestClient) -> tuple[str, int]:
    project_id = client.post(
        "/api/projects", json={"name": "Placement queued python"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Docs")
    transcript = project.add_column(sheet_id, "transcript", type="text")
    project.add_rows(
        sheet_id,
        [{"transcript": "one two three four five six seven"}],
        {"transcript": transcript},
    )
    return project_id, sheet_id


def test_map_python_queued_launch_returns_before_sandbox_runs(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project_id, sheet_id = _seed_python_project(client)

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_python_action(sheet_id),
    )
    assert response.status_code == 200, response.text
    result = ActionResult.model_validate(response.json())

    # HARD CONSTRAINT: launch returns the run handle immediately; the sandbox
    # has not executed yet -- no output column exists.
    assert result.status == "queued"
    assert result.action.kind == "map.python"
    assert result.run_id is not None
    assert result.job_id is not None
    assert result.receipt_id is not None

    # HARD CONSTRAINT: the sandbox has not executed -- the output column shell
    # is reserved/claimed synchronously (request-time precheck), but no value
    # has been written for it yet.
    project = client.app.state.workspace.get(project_id)
    excerpt_column = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name=? AND hidden=0",
        (sheet_id, "excerpt"),
    ).fetchone()
    assert excerpt_column is not None
    values_before = project.get_values(sheet_id, int(excerpt_column["id"]))
    assert not any(value is not None for value in values_before.values())

    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.payload["action_kind"] == "map.python"
    assert job.payload["run_id"] == result.run_id
    assert job.payload["v1_cache_mode"] == "replay"
    assert "v1_input_column_ids" in job.payload
    status_before = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    ).json()["run"]["public_status"]
    assert status_before["status"] == "queued"

    # Execution happens on the worker, not the request.
    drain_queue(client)

    status_after = client.get(
        f"/api/projects/{project_id}/actions/runs/{result.run_id}/status"
    ).json()["run"]["public_status"]
    assert status_after["status"] == "completed"

    receipt_row = project.db.execute(
        "SELECT status FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["status"] == "completed"

    values_after = project.get_values(sheet_id, int(excerpt_column["id"]))
    assert list(values_after.values())[0] == "one two three"


def test_hosted_admission_refuses_map_python_before_lookup_or_runner(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class HostedAdmission:
        def __init__(self) -> None:
            self.bodies: list[dict[str, Any]] = []

        def execution_denied(self, *, body: dict[str, Any]) -> bool:
            self.bodies.append(body)
            return body.get("action_kind") == "map.python"

    client = _client(tmp_path)
    project_id, sheet_id = _seed_python_project(client)
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_python_action(
            sheet_id,
            idempotency_key="hosted-map-python-refusal@sha256:stable",
        ),
    )
    result = ActionResult.model_validate(response.json())
    assert result.status == "queued"
    assert result.job_id is not None

    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("hosted admission must precede MapRunner lookup")

    monkeypatch.setattr("frisket.engine.runner.validation.recipe_for_spec", unexpected)
    monkeypatch.setattr("frisket.engine.jobs.runs.MapRunner", unexpected)
    admission = HostedAdmission()
    registry = HandlerRegistry()
    register_project_run_handler(
        registry,
        workspace_root=client.app.state.workspace.root,
        router=ModelRouter(cache=None, cache_mode="off"),
        worker_ports=WorkerPorts(admission_port=admission),
    )
    assert Worker(
        client.app.state.workspace.queue,
        registry,
        worker_id="hosted-map-python-refusal",
    ).run_once()

    project = client.app.state.workspace.get(project_id)
    receipt = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt is not None
    body = json.loads(receipt["body"])
    assert body["status"] == "failed"
    assert body["errors"][0]["code"] == "code_action_disabled"
    assert [candidate["action_kind"] for candidate in admission.bodies] == [
        "map.python"
    ]


def test_queued_run_freezes_the_runtime_mode_that_admitted_it(tmp_path: Path) -> None:
    client = _client(tmp_path)
    project_id, sheet_id = _seed_python_project(client)
    changed = client.patch(
        "/api/config",
        json={"cache_mode": "replay_strict", "confirmed": False},
    )
    assert changed.status_code == 200, changed.text

    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_python_action(sheet_id),
    )
    result = ActionResult.model_validate(response.json())
    assert result.job_id is not None
    job = client.app.state.workspace.queue.get(result.job_id)
    assert job is not None
    assert job.payload["v1_cache_mode"] == "replay_strict"
