from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from frisket.ai.llm import ModelRouter
from frisket.engine.store import Project

PROJECT_ID = "project-edge-materializer"
VECS = {
    "Acme Corporation": [1.0, 0.0, 0.0],
    "Globex LLC": [0.0, 1.0, 0.0],
    "Initech Inc": [0.0, 0.0, 1.0],
    "ACME Corp": [1.0, 0.0, 0.0],
    "Globex": [0.6258, 0.78, 0.0],
    "Umbrella Holdings": [0.5774, 0.5774, 0.5774],
}


def _fake_embed(calls: list[list[str]]):
    def embed(texts: list[str]) -> list[list[float]]:
        calls.append(list(texts))
        return [VECS[text] for text in texts]

    return embed


def _index_names(project: Project) -> set[str]:
    return {
        row["name"]
        for row in project.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }


def _membership_schema_sql(project: Project) -> str:
    row = project.db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='materialized_row_sources'"
    ).fetchone()
    assert row is not None
    return str(row["sql"])


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


def test_materialized_row_sources_schema_is_created_on_a_fresh_bundle(
    tmp_path: Path,
):
    """The CHECK constraint set and the three indexes come from
    ``Project.create`` and nowhere else -- the open-time replay and the
    CHECK-drift rebuild that used to repair them in place are gone, and a
    bundle that does not match ``schema.py`` is now refused, not converted."""
    project = Project.create(tmp_path / "schema.frisket", name="schema")
    required_indexes = {
        "idx_materialized_row_sources_source",
        "idx_materialized_row_sources_materialized",
        "idx_materialized_row_sources_op",
    }
    try:
        assert "edge_source" in _membership_schema_sql(project)
        assert "edge_target" in _membership_schema_sql(project)
        assert "aggregate_source" in _membership_schema_sql(project)
        assert "ON DELETE CASCADE" in _membership_schema_sql(project)
        assert required_indexes.issubset(_index_names(project))

        source_sheet = project.add_sheet("Source")
        source_col = project.add_column(source_sheet, "name")
        source_row = project.add_rows(
            source_sheet, [{"name": "source"}], {"name": source_col}
        )[0]
        child_sheet = project.add_sheet("Child", parent_sheet_id=source_sheet)
        child_col = project.add_column(child_sheet, "name")
        child_row = project.add_rows(
            child_sheet, [{"name": "child"}], {"name": child_col}
        )[0]
        op_id = project.append_op("test.materialized_row_sources")
        project.db.execute(
            "INSERT INTO materialized_row_sources "
            "(materialized_row_id, source_row_id, source_sheet_id, op_id, role) "
            "VALUES (?, ?, ?, ?, ?)",
            (child_row, source_row, source_sheet, op_id, "edge_source"),
        )
        project.db.commit()
        try:
            project.db.execute(
                "INSERT INTO materialized_row_sources "
                "(materialized_row_id, source_row_id, source_sheet_id, op_id, role) "
                "VALUES (?, ?, ?, ?, ?)",
                (child_row, source_row, source_sheet, op_id, "parent"),
            )
        except sqlite3.IntegrityError:
            project.db.rollback()
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError("invalid membership role was accepted")
        try:
            project.db.execute(
                "INSERT INTO materialized_row_sources "
                "(materialized_row_id, source_row_id, source_sheet_id, op_id, role) "
                "VALUES (?, ?, ?, ?, ?)",
                (child_row, source_row, source_sheet, op_id, "edge_source"),
            )
        except sqlite3.IntegrityError:
            project.db.rollback()
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError("duplicate membership row was accepted")

        project.db.execute("DELETE FROM rows WHERE id=?", (child_row,))
        project.db.commit()
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM materialized_row_sources"
            ).fetchone()[0]
            == 0
        )
    finally:
        project.close()


def _seed_project(project_path: Path) -> tuple[int, int, list[int], list[int]]:
    project = Project.create(project_path, name="Edge Materializer")
    try:
        source_sheet_id = project.add_sheet("Donors")
        source_columns = {
            "donor": project.add_column(source_sheet_id, "donor", type="text")
        }
        source_row_ids = project.add_rows(
            source_sheet_id,
            [
                {"donor": "ACME Corp"},
                {"donor": "Globex"},
                {"donor": "Umbrella Holdings"},
            ],
            source_columns,
        )
        target_sheet_id = project.add_sheet("Registry")
        target_columns = {
            "company": project.add_column(target_sheet_id, "company", type="text")
        }
        target_row_ids = project.add_rows(
            target_sheet_id,
            [
                {"company": "Acme Corporation"},
                {"company": "Globex LLC"},
                {"company": "Initech Inc"},
            ],
            target_columns,
        )
        return source_sheet_id, target_sheet_id, source_row_ids, target_row_ids
    finally:
        project.close()


def _join_action(
    source_sheet_id: int,
    target_sheet_id: int,
    *,
    consented_promise_set_hash: str | None = None,
) -> dict[str, Any]:
    action = {
        "action_id": "join.semantic",
        "scope": {"kind": "sheet_rows", "sheet_id": source_sheet_id},
        "sheet_name": "Raw Semantic Links",
        "output_names": {
            "source": "donor",
            "match_value": "company_match_value",
            "match_score": "company_match_score",
            "matched_row_id": "company_match_row_id",
        },
        "params": {
            "source": "donor",
            "target": {"sheet_id": target_sheet_id, "column": "company"},
            "match_threshold": 0.70,
            "confident_threshold": 0.85,
        },
        "idempotency_key": "join_semantic@edge-materializer",
    }
    if consented_promise_set_hash is not None:
        action["confirmation"] = consented_promise_set_hash
    return action


def _derive_link_action(receipt_id: str) -> dict[str, Any]:
    return {
        "action_id": "derive.link_table",
        "scope": {"kind": "project"},
        "sheet_name": "Reviewed Semantic Links",
        "params": {
            "source": {"kind": "semantic_join", "receipt_id": receipt_id},
            "include_unmatched": False,
        },
        "idempotency_key": "derive_link_table@edge-materializer",
    }


def test_derive_link_table_writes_canonical_edge_membership_and_replays_it(
    tmp_path: Path,
    monkeypatch,
):
    from frisket.engine.executor import actions as executor_actions

    project_path = tmp_path / "edge.frisket"
    source_sheet_id, target_sheet_id, source_row_ids, target_row_ids = _seed_project(
        project_path
    )
    embed_calls: list[list[str]] = []
    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder",
        lambda router=None, **_kw: (_fake_embed(embed_calls), "stub/edge-materializer"),
    )

    project = Project(project_path)
    try:
        router = ModelRouter(cache=None, cache_mode="off")
        gated = executor_actions.run_action_spec(
            project,
            _join_action(source_sheet_id, target_sheet_id),
            project_id=PROJECT_ID,
            router=router,
        )
        assert gated.status == "needs_confirmation", gated.errors
        assert not embed_calls
        promise_set_hash = gated.errors[0].details["promise_set_hash"]

        join = executor_actions.run_action_spec(
            project,
            _join_action(
                source_sheet_id,
                target_sheet_id,
                consented_promise_set_hash=promise_set_hash,
            ),
            project_id=PROJECT_ID,
            router=router,
        )
        assert join.status == "completed", join.errors
        assert join.receipt_id is not None

        action = _derive_link_action(join.receipt_id)
        result = executor_actions.run_action_spec(
            project,
            action,
            project_id=PROJECT_ID,
        )
        assert result.status == "completed", result.errors
        child_row_ids = next(
            output.row_ids for output in result.outputs if output.kind == "rows"
        )
        assert child_row_ids == list(child_row_ids)

        rows = project.db.execute(
            "SELECT materialized_row_id, source_row_id, source_sheet_id, role "
            "FROM materialized_row_sources WHERE op_id=? "
            "ORDER BY materialized_row_id, role",
            (result.op_ids[0],),
        ).fetchall()
        assert len(rows) == len(child_row_ids) * 2
        by_child = {
            int(row_id): {
                row["role"]: (int(row["source_row_id"]), int(row["source_sheet_id"]))
                for row in rows
                if int(row["materialized_row_id"]) == int(row_id)
            }
            for row_id in child_row_ids
        }
        assert [by_child[row_id]["edge_source"][0] for row_id in child_row_ids] == (
            source_row_ids[: len(child_row_ids)]
        )
        assert [by_child[row_id]["edge_target"][0] for row_id in child_row_ids] == (
            target_row_ids[: len(child_row_ids)]
        )
        assert {entry["edge_source"][1] for entry in by_child.values()} == {
            source_sheet_id
        }
        assert {entry["edge_target"][1] for entry in by_child.values()} == {
            target_sheet_id
        }

        receipt_row = project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()
        assert receipt_row is not None
        receipt = json.loads(receipt_row["body"])
        refs = [
            item["ref"]
            for section in ("inputs", "outputs", "evidence")
            for item in receipt[section]
        ]
        action_refs = [output.ref for output in result.outputs]
        for kind in ("materialized_sheet", "materialized_rows"):
            assert next(ref for ref in refs if ref["kind"] == kind) == next(
                ref for ref in action_refs if ref["kind"] == kind
            )
        membership_ref = next(
            ref for ref in refs if ref["kind"] == "materialized_row_sources"
        )
        assert membership_ref["op_id"] == result.op_ids[0]
        assert membership_ref["row_count"] == len(rows)
        assert {row["role"] for row in membership_ref["rows"]} == {
            "edge_source",
            "edge_target",
        }

        replay = executor_actions.run_action_spec(
            project,
            action,
            project_id=PROJECT_ID,
        )
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == result.receipt_id
        assert [output.ref for output in replay.outputs] == [
            output.ref for output in result.outputs
        ]

        original_body = _mutate_receipt_ref(
            project,
            result.receipt_id,
            "materialized_rows",
            lambda ref: ref.__setitem__("row_ids", ["not-an-int"]),
        )
        stale_from_malformed_receipt = executor_actions.run_action_spec(
            project,
            action,
            project_id=PROJECT_ID,
        )
        assert stale_from_malformed_receipt.status == "failed"
        assert stale_from_malformed_receipt.errors[0].code == "stale_replay"
        _replace_receipt_body(project, result.receipt_id, original_body)

        original_body = _mutate_receipt_ref(
            project,
            result.receipt_id,
            "materialized_rows",
            lambda ref: ref.__setitem__("op_id", result.op_ids[0] + 1),
        )
        stale_from_op_drift = executor_actions.run_action_spec(
            project,
            action,
            project_id=PROJECT_ID,
        )
        assert stale_from_op_drift.status == "failed"
        assert stale_from_op_drift.errors[0].code == "stale_replay"
        _replace_receipt_body(project, result.receipt_id, original_body)

        first_edge_target = next(row for row in rows if row["role"] == "edge_target")
        project.db.execute(
            "DELETE FROM materialized_row_sources "
            "WHERE op_id=? AND role='edge_target' "
            "AND materialized_row_id=? AND source_row_id=?",
            (
                result.op_ids[0],
                int(first_edge_target["materialized_row_id"]),
                int(first_edge_target["source_row_id"]),
            ),
        )
        project.db.commit()
        stale = executor_actions.run_action_spec(
            project,
            action,
            project_id=PROJECT_ID,
        )
        assert stale.status == "failed"
        assert stale.errors[0].code == "stale_replay"
    finally:
        project.close()
