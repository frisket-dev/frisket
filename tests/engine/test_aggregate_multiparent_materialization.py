from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.store import Project

PROJECT_ID = "project-aggregate-materializer"


class _SummaryAdapter:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.requests.append(req)
        prompt = str(req.messages[-1]["content"])
        summary = (
            "Accountability stories share contracting risk."
            if "accountability" in prompt
            else "Infrastructure stories share service disruption risk."
        )
        return LLMResponse(
            content=summary,
            data=None,
            tokens_in=83,
            tokens_out=17,
            cost=0.006,
            model=req.model,
        )


def _summary_router() -> tuple[ModelRouter, _SummaryAdapter]:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    adapter = _SummaryAdapter()
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return router, adapter


def _receipt_refs(project: Project, receipt_id: str) -> list[dict[str, Any]]:
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    assert row is not None
    body = json.loads(row["body"])
    refs: list[dict[str, Any]] = []
    for section in ("inputs", "outputs", "evidence"):
        for item in body.get(section) or []:
            ref = item.get("ref") if isinstance(item, dict) else None
            if isinstance(ref, dict):
                refs.append(ref)
    return refs


def _replace_receipt_body(
    project: Project, receipt_id: str, body: dict[str, Any]
) -> None:
    project.db.execute(
        "UPDATE receipts SET body=? WHERE id=?",
        (json.dumps(body, sort_keys=True), receipt_id),
    )
    project.db.commit()


def _mutate_receipt_ref(
    project: Project,
    receipt_id: str,
    kind: str,
    mutate: Any,
) -> dict[str, Any]:
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    assert row is not None
    original_body = json.loads(row["body"])
    body = json.loads(row["body"])
    for section in ("inputs", "outputs", "evidence"):
        for item in body.get(section) or []:
            ref = item.get("ref") if isinstance(item, dict) else None
            if isinstance(ref, dict) and ref.get("kind") == kind:
                mutate(ref)
                _replace_receipt_body(project, receipt_id, body)
                return original_body
    raise AssertionError(f"receipt ref {kind} not found")


def _membership_rows(project: Project, op_id: int) -> list[dict[str, Any]]:
    return [
        {
            "materialized_row_id": int(row["materialized_row_id"]),
            "source_row_id": int(row["source_row_id"]),
            "source_sheet_id": int(row["source_sheet_id"]),
            "role": row["role"],
        }
        for row in project.db.execute(
            "SELECT materialized_row_id, source_row_id, source_sheet_id, role "
            "FROM materialized_row_sources WHERE op_id=? "
            "ORDER BY materialized_row_id, source_row_id, role",
            (op_id,),
        ).fetchall()
    ]


def _seed_entities_project(tmp_path: Path) -> tuple[Project, int, list[int]]:
    project = Project.create(tmp_path / "entities.frisket", name="entities")
    sheet_id = project.add_sheet("People")
    name_col = project.add_column(sheet_id, "name", type="text")
    row_ids = project.add_rows(
        sheet_id,
        [
            {"name": "Jon Smith"},
            {"name": "Smith, Jon"},
            {"name": "Jon Smith"},
            {"name": "Jane Doe"},
        ],
        {"name": name_col},
    )
    return project, sheet_id, row_ids


def _cluster_action(sheet_id: int) -> dict[str, Any]:
    return {
        "action_id": "cluster.values",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "output_names": {"canonical": "name_canonical"},
        "params": {
            "source": "name",
            "method": "fingerprint",
            "min_size": 2,
        },
        "idempotency_key": "cluster_values@aggregate-test",
    }


def _resolve_action(receipt_id: str) -> dict[str, Any]:
    return {
        "action_id": "resolve.entities",
        "scope": {"kind": "project"},
        "sheet_name": "Entities",
        "output_names": {
            key: key
            for key in ("entity", "key", "variants", "mentions", "source_variants")
        },
        "params": {
            "source": {"kind": "cluster_values", "receipt_id": receipt_id},
        },
        "idempotency_key": "resolve_entities@aggregate-test",
    }


def test_resolve_entities_writes_aggregate_membership_not_fake_parentage(
    tmp_path: Path,
):
    from frisket.engine.executor import actions as executor_actions

    project, sheet_id, row_ids = _seed_entities_project(tmp_path)
    try:
        cluster = executor_actions.run_action_spec(
            project,
            _cluster_action(sheet_id),
            project_id=PROJECT_ID,
        )
        assert cluster.status == "completed", cluster.errors
        assert cluster.receipt_id is not None
        result = executor_actions.run_action_spec(
            project,
            _resolve_action(cluster.receipt_id),
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors
        entity_rows_ref = next(
            ref
            for ref in _receipt_refs(project, result.receipt_id)
            if ref["kind"] == "materialized_rows"
        )
        entity_row_ids = entity_rows_ref["row_ids"]
        parentage = project.db.execute(
            "SELECT id, parent_row_id FROM rows "
            f"WHERE id IN ({','.join('?' for _ in entity_row_ids)})",
            entity_row_ids,
        ).fetchall()
        assert {row["parent_row_id"] for row in parentage} == {None}

        membership = _membership_rows(project, result.op_ids[0])
        assert {row["role"] for row in membership} == {"aggregate_source"}
        source_variants_column = next(
            column
            for column in project.columns(result.outputs[0].sheet_id)
            if column["name"] == "source_variants"
        )
        source_variants = project.get_values(
            result.outputs[0].sheet_id, source_variants_column["id"]
        )
        expected_pairs = {
            (entity_row_id, source_row_id)
            for entity_row_id in entity_row_ids
            for variant in source_variants[entity_row_id]
            for source_row_id in variant["row_ids"]
        }
        assert {
            (row["materialized_row_id"], row["source_row_id"]) for row in membership
        } == expected_pairs
        membership_ref = next(
            ref
            for ref in _receipt_refs(project, result.receipt_id)
            if ref["kind"] == "materialized_row_sources"
        )
        assert membership_ref["row_count"] == len(membership)

        project.db.execute(
            "UPDATE rows SET parent_row_id=? WHERE id=?",
            (row_ids[0], entity_row_ids[0]),
        )
        project.db.commit()
        stale = executor_actions.run_action_spec(
            project,
            _resolve_action(cluster.receipt_id),
            project_id=PROJECT_ID,
        )
        assert stale.status == "failed"
        assert stale.errors[0].code == "stale_replay"
    finally:
        project.close()


def _seed_reduce_project(tmp_path: Path) -> tuple[Project, int, list[int]]:
    project = Project.create(tmp_path / "reduce.frisket", name="reduce")
    sheet_id = project.add_sheet("Feature Tour")
    columns = {
        "story": project.add_column(sheet_id, "story", type="text"),
        "beat": project.add_column(sheet_id, "beat", type="text"),
        "risk_score": project.add_column(sheet_id, "risk_score", type="integer"),
    }
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "story": "City hall awarded a no-bid software contract",
                "beat": "accountability",
                "risk_score": 8,
            },
            {
                "story": "Audit found duplicate vendor payments",
                "beat": "accountability",
                "risk_score": 9,
            },
            {
                "story": "Water main repairs closed two blocks",
                "beat": "infrastructure",
                "risk_score": 4,
            },
        ],
        columns,
    )
    return project, sheet_id, row_ids


def _reduce_action(sheet_id: int) -> dict[str, Any]:
    return {
        "action_id": "reduce.group_summary",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "sheet_name": "Beat Summaries",
        "output_names": {},
        "params": {
            "source": ["story", "risk_score"],
            "group_by": "beat",
            "model": "anthropic/claude-haiku-4-5",
            "instruction": "Summarize common reporting risks for each beat.",
        },
        "idempotency_key": "reduce_summary@aggregate-test",
    }


def test_reduce_group_summary_writes_aggregate_membership_and_replays_it(
    tmp_path: Path,
):
    from runner_test_helpers import run_action_with_exact_confirmation

    project, sheet_id, source_row_ids = _seed_reduce_project(tmp_path)
    router, adapter = _summary_router()
    try:
        action = _reduce_action(sheet_id)
        result = run_action_with_exact_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
            router=router,
        )
        assert result.status == "completed", result.errors
        group_ref = next(
            ref
            for ref in _receipt_refs(project, result.receipt_id)
            if ref["kind"] == "reduce_group_summary_groups"
        )
        summary_row_ids = [group["summary_row_id"] for group in group_ref["groups"]]
        parentage = project.db.execute(
            "SELECT id, parent_row_id FROM rows "
            f"WHERE id IN ({','.join('?' for _ in summary_row_ids)})",
            summary_row_ids,
        ).fetchall()
        assert {row["parent_row_id"] for row in parentage} == {None}

        membership = _membership_rows(project, result.op_ids[0])
        assert {row["role"] for row in membership} == {"aggregate_source"}
        expected_pairs = {
            (group["summary_row_id"], source_row_id)
            for group in group_ref["groups"]
            for source_row_id in group["source_row_ids"]
        }
        assert {
            (row["materialized_row_id"], row["source_row_id"]) for row in membership
        } == expected_pairs
        assert {row["source_row_id"] for row in membership} == set(source_row_ids)
        membership_ref = next(
            ref
            for ref in _receipt_refs(project, result.receipt_id)
            if ref["kind"] == "materialized_row_sources"
        )
        assert membership_ref["row_count"] == len(membership)
        summary_column_id = project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='summary'",
            (group_ref["summary_sheet_id"],),
        ).fetchone()["id"]
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM cells WHERE column_id=?",
                (summary_column_id,),
            ).fetchone()[0]
            == 0
        )
        assert set(
            project.get_values(
                group_ref["summary_sheet_id"],
                int(summary_column_id),
                row_ids=summary_row_ids,
            ).values()
        ) == {
            "Accountability stories share contracting risk.",
            "Infrastructure stories share service disruption risk.",
        }

        replay = run_action_with_exact_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
            router=router,
        )
        assert replay.status == "completed"
        assert len(adapter.requests) == 2

        original_body = _mutate_receipt_ref(
            project,
            result.receipt_id,
            "materialized_rows",
            lambda ref: ref.__setitem__("row_ids", ["not-an-int"]),
        )
        stale_from_malformed_rows = run_action_with_exact_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
            router=router,
        )
        assert stale_from_malformed_rows.status == "failed"
        assert stale_from_malformed_rows.errors[0].code == "stale_replay"
        _replace_receipt_body(project, result.receipt_id, original_body)

        original_body = _mutate_receipt_ref(
            project,
            result.receipt_id,
            "reduce_group_summary_groups",
            lambda ref: ref["groups"][0].__setitem__("source_row_ids", ["bad"]),
        )
        stale_from_malformed_group = run_action_with_exact_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
            router=router,
        )
        assert stale_from_malformed_group.status == "failed"
        assert stale_from_malformed_group.errors[0].code == "stale_replay"
        _replace_receipt_body(project, result.receipt_id, original_body)

        first_membership = membership[0]
        project.db.execute(
            "DELETE FROM materialized_row_sources "
            "WHERE op_id=? AND materialized_row_id=? AND source_row_id=? "
            "AND role='aggregate_source'",
            (
                result.op_ids[0],
                first_membership["materialized_row_id"],
                first_membership["source_row_id"],
            ),
        )
        project.db.commit()
        stale = run_action_with_exact_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
            router=router,
        )
        assert stale.status == "failed"
        assert stale.errors[0].code == "stale_replay"
        assert len(adapter.requests) == 2
    finally:
        project.close()
