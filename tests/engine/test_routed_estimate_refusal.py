"""An unavailable routed execution must not mint an alternate unrouted quote."""

import pytest

from frisket.actions.system import typed_action_for_request
from frisket.engine.executor.map_rows_action import _typed_map_rows_plan
from frisket.engine.runner import validation
from frisket.engine.store import Project
from frisket.execution.resolver import Refusal


@pytest.mark.parametrize(
    "action_id",
    ["enrich.geocode", "enrich.census_demographics", "media.ocr", "media.transcribe"],
)
@pytest.mark.parametrize("family", ["no_live_target", "no_capable_target", "unfunded"])
def test_shared_estimate_preserves_admitted_refusal_without_fallback(
    tmp_path, monkeypatch, action_id, family
):
    project = Project.create(tmp_path / "refused-estimate.frisket")
    try:
        sheet_id = project.add_sheet("Sources")
        column_id = project.add_column(sheet_id, "source", type="text")
        project.add_rows(sheet_id, [{"source": "selected"}], {"source": column_id})
        plan = _typed_map_rows_plan(
            typed_action_for_request(
                {
                    "action_id": action_id,
                    "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                    "params": {"source": "source"},
                    "idempotency_key": "refused-estimate",
                }
            )
        )
        program, spec = plan.program, plan.spec_dict()
        refusal = Refusal(family=family, remedy="Select an available execution target.")

        def forbidden(*args, **kwargs):
            pytest.fail(
                "A refused route must not read inputs or estimate another route"
            )

        monkeypatch.setattr(program, "estimate", forbidden)
        monkeypatch.setattr(validation, "row_values", forbidden)
        monkeypatch.setattr(validation, "resolve_for_action", forbidden)
        before = project.db.total_changes
        with pytest.raises(validation.ExecutionResolutionRefused) as caught:
            validation.estimate_run(project, spec, program=program, resolution=refusal)
        assert caught.value.refusal is refusal
        assert caught.value.family == family
        assert str(caught.value) == refusal.remedy
        assert project.db.total_changes == before
    finally:
        project.close()
