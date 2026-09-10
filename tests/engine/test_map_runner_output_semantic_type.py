"""``preparation.prepare_validated`` threads a recipe's declared
``semantic_type`` output-field key into ``project.add_column`` for a
freshly-created output column, while a reused published output keeps its
declared contract immutable. Exercised directly against
``prepare_validated`` (the writing half of ``MapRunner._prepare``) with a
minimal fake recipe, bypassing the LLM/registry machinery entirely: no spec
resolution or row execution is needed to prove the output-column
side-effects.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from frisket.ops.base import Recipe
from frisket.engine.runner import preparation
from frisket.engine.runner.validation import _ValidatedSpec
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.runner.result_generations import declare_prepared_outputs
from frisket.engine.store.result_generations import ResultGenerationStore


@dataclass
class _FakeSemanticRecipe(Recipe):
    """A minimal non-LLM recipe whose single declared output field carries
    whatever ``semantic_type`` the spec asks for (or omits it)."""

    consumes_resolution = False  # required declaration (Recipe)
    cost_class = "free"  # required declaration (Recipe)

    name: str = "test.fake_semantic"
    llm: bool = False

    def output_fields(self, spec: dict) -> list[dict]:
        field: dict = {
            "name": spec["field_name"],
            "column_type": "text",
            "schema": {"type": "string"},
            "description": "",
        }
        if "semantic_type" in spec:
            field["semantic_type"] = spec["semantic_type"]
        return [field]


def _seed(p: Project) -> tuple[int, list[int]]:
    sheet = p.add_sheet("data")
    src = p.add_column(sheet, "text")
    row_ids = p.add_rows(sheet, [{"text": "a"}, {"text": "b"}], {"text": src})
    return sheet, row_ids


def _validated(
    project: Project,
    sheet_id: int,
    row_ids: list[int],
    spec: dict,
) -> _ValidatedSpec:
    columns = project.columns(sheet_id)
    recipe = _FakeSemanticRecipe()
    return _ValidatedSpec(
        recipe=recipe,
        sheet_id=sheet_id,
        columns=columns,
        col_map={c["name"]: c["id"] for c in columns},
        row_ids=row_ids,
        output_fields=recipe.output_fields(spec),
        est=None,
        has_stored_run_scope=False,
        stored_run_scope_count=None,
        explicit_resume_scope=None,
    )


class TestFreshOutputColumnSemanticType:
    def test_declared_semantic_type_lands_on_the_new_column(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        sheet_id, row_ids = _seed(p)
        run_store = RunResultStore(p)
        spec = {
            "action_kind": "test.fake_semantic",
            "sheet_id": sheet_id,
            "field_name": "mentions",
            "semantic_type": "entity_mentions",
        }
        prepared = preparation.prepare_validated(
            p,
            run_store,
            spec,
            validated=_validated(p, sheet_id, row_ids, spec),
            resume_run_id=None,
            defer_commits=False,
        )
        col_id = prepared.out_cols["mentions"]
        assert p.get_column(col_id)["semantic_type"] == "entity_mentions"
        p.close()

    def test_omitted_semantic_type_leaves_the_new_column_null(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        sheet_id, row_ids = _seed(p)
        run_store = RunResultStore(p)
        spec = {
            "action_kind": "test.fake_semantic",
            "sheet_id": sheet_id,
            "field_name": "plain",
        }
        prepared = preparation.prepare_validated(
            p,
            run_store,
            spec,
            validated=_validated(p, sheet_id, row_ids, spec),
            resume_run_id=None,
            defer_commits=False,
        )
        col_id = prepared.out_cols["plain"]
        assert p.get_column(col_id)["semantic_type"] is None
        p.close()


class TestReuseConvergesSemanticType:
    """~L152-157: a re-run onto an EXISTING ai_generated output column must
    converge the stored semantic_type onto whatever the recipe declares NOW
    -- not keep a stale marker, in either direction (adding one, changing
    one, or clearing one)."""

    def _run(self, p: Project, sheet_id: int, row_ids: list[int], spec: dict):
        run_store = RunResultStore(p)
        prepared = preparation.prepare_validated(
            p,
            run_store,
            spec,
            validated=_validated(p, sheet_id, row_ids, spec),
            resume_run_id=None,
            defer_commits=False,
        )
        token = f"output-claim:test-semantic:{prepared.run_id}"
        names = list(prepared.out_cols)
        claims, conflict = OutputColumnClaimStore(p).acquire(
            sheet_id=sheet_id,
            output_names=names,
            action_kind="test.fake_semantic",
            run_id=prepared.run_id,
            claim_token=token,
        )
        assert conflict is None and len(claims) == len(names)
        OutputColumnClaimStore(p).bind_to_run(
            claim_token=token,
            run_id=prepared.run_id,
            expected_output_names=names,
        )
        declare_prepared_outputs(p, run_store, prepared, claim_token=token)
        run_store.finish_run(prepared.run_id, "completed")
        ResultGenerationStore(p).seal(
            prepared.run_id,
            prepared.out_cols.values(),
            claim_token=token,
            terminal_disposition="completed",
        )
        OutputColumnClaimStore(p).release(claim_token=token)
        return prepared

    def test_second_run_adds_a_semantic_type_the_first_run_lacked(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        sheet_id, row_ids = _seed(p)
        base_spec = {
            "action_kind": "test.fake_semantic",
            "sheet_id": sheet_id,
            "field_name": "mentions",
        }
        first = self._run(p, sheet_id, row_ids, dict(base_spec))
        col_id = first.out_cols["mentions"]
        assert p.get_column(col_id)["semantic_type"] is None

        with pytest.raises(ValueError, match="keeps the output contract"):
            self._run(
                p, sheet_id, row_ids, {**base_spec, "semantic_type": "entity_mentions"}
            )
        assert p.get_column(col_id)["semantic_type"] is None
        p.close()

    def test_second_run_changes_an_existing_semantic_type(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        sheet_id, row_ids = _seed(p)
        base_spec = {
            "action_kind": "test.fake_semantic",
            "sheet_id": sheet_id,
            "field_name": "mentions",
        }
        first = self._run(
            p, sheet_id, row_ids, {**base_spec, "semantic_type": "entity_mentions"}
        )
        col_id = first.out_cols["mentions"]
        assert p.get_column(col_id)["semantic_type"] == "entity_mentions"

        with pytest.raises(ValueError, match="keeps the output contract"):
            self._run(
                p,
                sheet_id,
                row_ids,
                {**base_spec, "semantic_type": "some_other_contract"},
            )
        assert p.get_column(col_id)["semantic_type"] == "entity_mentions"
        p.close()

    def test_second_run_clears_a_semantic_type_no_longer_declared(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        sheet_id, row_ids = _seed(p)
        base_spec = {
            "action_kind": "test.fake_semantic",
            "sheet_id": sheet_id,
            "field_name": "mentions",
        }
        first = self._run(
            p, sheet_id, row_ids, {**base_spec, "semantic_type": "entity_mentions"}
        )
        col_id = first.out_cols["mentions"]
        assert p.get_column(col_id)["semantic_type"] == "entity_mentions"

        with pytest.raises(ValueError, match="keeps the output contract"):
            self._run(p, sheet_id, row_ids, dict(base_spec))
        assert p.get_column(col_id)["semantic_type"] == "entity_mentions"
        p.close()
