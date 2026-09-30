from __future__ import annotations

from pathlib import Path
from typing import Any

from frisket.actions.types import ActionRequest
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.map_find_source import resolve_find_sources
from frisket.engine.runner.review import (
    queue_count,
    review_bundle_page,
    review_runs_page,
    set_review_run_status,
)
from frisket.engine.store import Project
from frisket.engine.store.evidence import (
    list_cell_evidence,
    list_row_evidence,
    resolve_evidence_viewer,
)
from test_map_find_runtime import (
    _FindAdapter,
    _operation_for_request,
    _queued_find,
    _run_queued_find,
)


def _decision(
    *,
    run_id: int,
    row_id: int,
    column_id: int,
    decision: str,
    key: str,
    value: Any | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "run_id": run_id,
        "row_id": row_id,
        "column_id": column_id,
        "decision": decision,
    }
    if value is not None:
        params["value"] = value
    return {
        "action_id": "review.decision",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": key,
    }


def test_find_occurrences_publish_reviewable_grounded_result_heads(
    tmp_path: Path,
) -> None:
    project = Project.create(tmp_path / "find-review.frisket", name="Find review")
    try:
        source_sheet_id = project.add_sheet("Sources")
        body_column_id = project.add_column(source_sheet_id, "body", "text")
        source_row_id = project.add_rows(
            source_sheet_id,
            [
                {
                    "body": (
                        "China trade policy changed this year. "
                        "A second hearing followed the announcement."
                    )
                }
            ],
            {"body": body_column_id},
        )[0]

        def publish(*, key: str, matches: list[dict[str, Any]]) -> tuple[Any, int]:
            action = ActionRequest.model_validate(
                {
                    "action_id": "map.find",
                    "scope": {
                        "kind": "sheet_rows",
                        "sheet_id": source_sheet_id,
                        "row_ids": [source_row_id],
                    },
                    "sheet_name": "Findings",
                    "output_names": {
                        "match": "Match",
                        "source": "Source",
                        "metadata": "Metadata",
                    },
                    "params": {
                        "source": "body",
                        "instruction": "Find the two explicit findings.",
                        "fields": [
                            {"name": "source", "type": "list"},
                            {"name": "metadata", "type": "json"},
                        ],
                        "model": "anthropic/claude-haiku-4-5",
                    },
                    "idempotency_key": key,
                }
            )
            operation = _operation_for_request(action)
            sources = resolve_find_sources(project, operation)
            assert isinstance(sources, tuple)
            unit_id = str(sources[0].units[0].unit_id)
            envelope = _queued_find(project, action, project_id="project-find-review")
            result = _run_queued_find(
                project,
                envelope,
                _FindAdapter(
                    unit_id,
                    matches=[
                        {**match, "source_unit_ids": [unit_id]} for match in matches
                    ],
                ),
            )
            assert result.status == "completed", result.errors
            assert result.run_id is not None
            output_sheet_id = next(
                output.sheet_id for output in result.outputs if output.kind == "sheet"
            )
            assert output_sheet_id is not None
            return result, output_sheet_id

        first_matches = [
            {
                "match": "China trade policy",
                "source_unit_ids": [],
                "details": {
                    "source": ["policy", "trade"],
                    "metadata": {"topic": "China", "priority": 1},
                },
            },
            {
                "match": "second hearing",
                "source_unit_ids": [],
                "details": {
                    "source": ["hearing"],
                    "metadata": {"topic": "oversight", "priority": 2},
                },
            },
        ]
        first, findings_sheet_id = publish(
            key="find-review@sha256:first", matches=first_matches
        )
        run_id = int(first.run_id)
        columns = {
            str(column["name"]): int(column["id"])
            for column in project.columns(findings_sheet_id)
        }
        assert set(columns) == {"Match", "Source", "Metadata"}
        row_ids = project.visible_row_ids(findings_sheet_id)
        assert len(row_ids) == 2

        expected_values = {
            "Match": ["China trade policy", "second hearing"],
            "Source": [["policy", "trade"], ["hearing"]],
            "Metadata": [
                {"topic": "China", "priority": 1},
                {"topic": "oversight", "priority": 2},
            ],
        }
        for name, column_id in columns.items():
            values, refs = project.get_values_with_refs(
                findings_sheet_id, column_id, row_ids=row_ids
            )
            assert [values[row_id] for row_id in row_ids] == expected_values[name]
            assert [refs[row_id] for row_id in row_ids] == [
                {
                    "kind": "run_result",
                    "op_id": first.op_ids[0],
                    "row_id": row_id,
                    "column_id": column_id,
                    "run_id": run_id,
                }
                for row_id in row_ids
            ]

        # Occurrence rows are materialized membership only; every displayed
        # finding value comes from the run result that Review will decide.
        base_values = project.db.execute(
            "SELECT COUNT(*) FROM cells WHERE row_id IN (?, ?) AND column_id IN (?, ?, ?)",
            (*row_ids, *columns.values()),
        ).fetchone()[0]
        assert base_values == 0
        heads = project.db.execute(
            "SELECT row_id, column_id, run_id FROM cell_result_heads "
            "WHERE row_id IN (?, ?) AND column_id IN (?, ?, ?) ORDER BY row_id, column_id",
            (*row_ids, *columns.values()),
        ).fetchall()
        assert [tuple(row) for row in heads] == [
            (row_id, column_id, run_id)
            for row_id in row_ids
            for column_id in columns.values()
        ]

        page = review_bundle_page(project, run_id=run_id, include_reviewed=True)
        assert page["total"] == 2
        assert [bundle["row_id"] for bundle in page["bundles"]] == row_ids
        for bundle, row_id in zip(page["bundles"], row_ids, strict=True):
            assert [
                (field["column_name"], field["value"]) for field in bundle["fields"]
            ] == [
                (name, expected_values[name][row_ids.index(row_id)])
                for name in ("Match", "Source", "Metadata")
            ]

            row_evidence = list_row_evidence(
                project, sheet_id=findings_sheet_id, row_id=row_id
            )
            assert len(row_evidence["links"]) == 1
            for name, column_id in columns.items():
                cell_evidence = list_cell_evidence(
                    project,
                    sheet_id=findings_sheet_id,
                    row_id=row_id,
                    column_id=column_id,
                )
                assert len(cell_evidence["links"]) == 1
                assert cell_evidence["current_value_ref"]["kind"] == "run_result"
                viewer = resolve_evidence_viewer(
                    project, cell_evidence["links"][0]["stable_id"]
                )
                assert viewer["link"]["role"] == "primary_support"
                assert (
                    viewer["link"]["subject_ref"] == cell_evidence["current_value_ref"]
                )
                assert (
                    expected_values["Match"][row_ids.index(row_id)]
                    in viewer["artifacts"][0]["spans"][0]["quote"]
                )

        accepted = run_action_spec(
            project,
            _decision(
                run_id=run_id,
                row_id=row_ids[0],
                column_id=columns["Source"],
                decision="accept",
                key="find-review@sha256:accept",
            ),
            project_id="project-find-review",
        )
        assert accepted.status == "completed", accepted.errors
        edited = run_action_spec(
            project,
            _decision(
                run_id=run_id,
                row_id=row_ids[0],
                column_id=columns["Match"],
                decision="edit",
                value="Corrected policy finding",
                key="find-review@sha256:edit",
            ),
            project_id="project-find-review",
        )
        assert edited.status == "completed", edited.errors
        rejected = run_action_spec(
            project,
            _decision(
                run_id=run_id,
                row_id=row_ids[1],
                column_id=columns["Match"],
                decision="reject",
                key="find-review@sha256:reject",
            ),
            project_id="project-find-review",
        )
        assert rejected.status == "completed", rejected.errors
        assert queue_count(project, run_id=run_id) == 3
        [review_run] = review_runs_page(project, run_id=run_id)["runs"]
        assert review_run["total"] == {
            "eligible_count": 6,
            "reviewed_count": 3,
            "accepted_count": 1,
            "incorrect_count": 2,
            "unreviewed_count": 3,
            "confidence_count": 0,
        }
        assert set_review_run_status(project, run_id=run_id, status="complete") == {
            "schema_version": "frisket.review_run_status.v1",
            "run_id": run_id,
            "status": "complete",
            "review_completed_at": project.db.execute(
                "SELECT review_completed_at FROM runs WHERE id=?", (run_id,)
            ).fetchone()[0],
        }
        assert (
            set_review_run_status(project, run_id=run_id, status="open")["status"]
            == "open"
        )

        edited_evidence = list_cell_evidence(
            project,
            sheet_id=findings_sheet_id,
            row_id=row_ids[0],
            column_id=columns["Match"],
            include_stale=True,
        )
        assert edited_evidence["links"][0]["status"] == "stale"
        assert edited_evidence["stale_count"] == 1

        second, second_sheet_id = publish(
            key="find-review@sha256:second",
            matches=[
                {
                    "match": "second hearing",
                    "source_unit_ids": [],
                    "details": {
                        "source": ["hearing"],
                        "metadata": {"topic": "oversight", "priority": 2},
                    },
                }
            ],
        )
        assert second_sheet_id != findings_sheet_id
        assert review_bundle_page(project, run_id=run_id)["total"] == 0
        assert queue_count(project, run_id=run_id) == 0
        [current_run] = review_runs_page(project)["runs"]
        assert current_run["run_id"] == second.run_id
        stale = run_action_spec(
            project,
            _decision(
                run_id=run_id,
                row_id=row_ids[1],
                column_id=columns["Match"],
                decision="accept",
                key="find-review@sha256:stale",
            ),
            project_id="project-find-review",
        )
        assert stale.status == "failed"
        assert stale.errors[0].code == "review_target_not_found"
    finally:
        project.close()
