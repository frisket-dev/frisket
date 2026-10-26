from pathlib import Path

from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace


def test_formula_runs_through_normal_service_without_model_configuration(
    tmp_path: Path,
):
    workspace = Workspace(tmp_path / "workspace", enable_local_model_pull=False)
    workspace.create("Formula", project_id="formula")
    project = workspace.get("formula")
    sheet = project.add_sheet("Numbers")
    amount = project.add_column(sheet, "amount", type="number")
    rows = project.add_rows(sheet, [{"amount": 3}, {"amount": 8}], {"amount": amount})
    response = ActionRunService(workspace).run_action(
        "formula",
        {
            "action_id": "map.formula",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [rows[1]]},
            "params": {"formula": {"text": "{{amount}} * 2"}},
            "output_names": {"result": "doubled"},
            "idempotency_key": "formula-service",
        },
    )
    assert response.status_code == 200, response.payload
    assert response.payload["status"] == "completed", response.payload
    output = next(
        column for column in project.columns(sheet) if column["name"] == "doubled"
    )
    assert output["type"] == "number"
    values = project.get_values(sheet, output["id"], row_ids=rows)
    assert values[rows[1]] == 16
    assert values.get(rows[0]) is None
