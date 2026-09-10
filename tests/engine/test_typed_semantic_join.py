from __future__ import annotations

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.engine.executor.actions import _default_map_runner_factory
from frisket.engine.executor.semantic_join_action import run_typed_semantic_join_action
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore


def test_semantic_join_publishes_source_and_child_with_feedable_receipt(
    tmp_path, monkeypatch
):
    vectors = {"ACME": [1.0, 0.0], "Acme": [1.0, 0.0], "Globex": [0.0, 1.0]}
    monkeypatch.setattr(
        "frisket.semantic.resolve_embedder",
        lambda *a, **kw: (lambda texts: [vectors[t] for t in texts], "fastembed/test"),
    )
    project = Project.create(tmp_path / "semantic.frisket")
    try:
        left = project.add_sheet("Donors")
        left_cols = {name: project.add_column(left, name) for name in ("donor", "note")}
        [source_row] = project.add_rows(
            left, [{"donor": "ACME", "note": "carried"}], left_cols
        )
        right = project.add_sheet("Registry")
        company = project.add_column(right, "company")
        right_rows = project.add_rows(
            right, [{"company": "Acme"}, {"company": "Globex"}], {"company": company}
        )
        request = ActionRequest.model_validate(
            {
                "action_id": "join.semantic",
                "scope": {"kind": "sheet_rows", "sheet_id": left},
                "params": {
                    "source": "donor",
                    "target": {"sheet_id": right, "column": "company"},
                    "carry": ["note"],
                },
                "output_names": {
                    "source": "original",
                    "carry.note": "memo",
                    "match_value": "best",
                },
                "sheet_name": "Matches",
                "idempotency_key": "semantic-one",
            }
        )
        bound = BoundTypedActionRequest.bind(
            ACTION_REGISTRY.get("join.semantic"), request
        )
        result = run_typed_semantic_join_action(
            project, "test", bound, None, _default_map_runner_factory
        )
        assert result.status == "completed", result.model_dump(mode="json")
        outputs = {col["name"]: col for col in project.columns(left)}
        assert project.get_values(left, outputs["best"]["id"])[source_row] == "Acme"
        child = next(sheet for sheet in project.sheets() if sheet["name"] == "Matches")
        assert {col["name"] for col in project.columns(child["id"])} == {
            "original",
            "memo",
            "best",
            "match_score",
            "matched_row_id",
        }
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        refs = {
            item.ref.get("role"): item.ref
            for item in receipt.outputs
            if item.ref.get("kind") == "semantic_join_output_column"
        }
        assert set(refs) == {"match_value", "match_score", "target_row_id"}
        assert all("derive.link_table" in ref["may_feed"] for ref in refs.values())
        edge = next(
            item.ref
            for item in receipt.outputs
            if item.ref.get("kind") == "semantic_join_link_edges"
        )
        assert edge["source_row_ids"] == [source_row]
        assert edge["target_row_ids"] == [right_rows[0]]
    finally:
        project.close()
