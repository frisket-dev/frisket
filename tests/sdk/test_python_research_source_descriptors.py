from frisket.engine.store import Project
from frisket.actions.python import PythonParams
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import discover_references
from frisket.actions.row_research import ResearchParams
from frisket.actions.model_rows import RICH_COLUMN_TYPES


def test_python_and_research_use_typed_column_sources() -> None:
    research = ResearchParams(
        source=["headline", "document"],
        model="anthropic/claude-haiku-4-5",
        question={"text": "Research"},
    )
    assert [ref.column for ref in discover_references(research)] == [
        "headline",
        "document",
    ]
    params = PythonParams.model_validate(
        {
            "input_columns": ["headline", "document"],
            "code": "result = None",
            "return_schema": {"type": "null"},
            "output_routes": [
                {
                    "name": "result",
                    "path": "$",
                    "target": {"kind": "column", "type": "json"},
                }
            ],
        }
    )
    refs = discover_references(params)
    assert [ref.column for ref in refs] == ["headline", "document"]
    assert all(ref.accepted_column_types is None for ref in refs)
    assert RICH_COLUMN_TYPES == (
        "text",
        "timestamped_transcript",
        "category",
        "date",
        "number",
        "integer",
        "boolean",
        "json",
        "image",
    )


def test_descriptor_owns_catalog_metadata_and_recipe_has_no_source_override() -> None:
    (python_requirement,) = ACTION_REGISTRY.get("map.python").catalog_entry()[
        "ui_hints"
    ]["source_requirements"]
    research_requirement = next(
        item
        for item in ACTION_REGISTRY.get("research.answer").catalog_entry()["ui_hints"][
            "source_requirements"
        ]
        if item["param"] == "source"
    )
    assert (python_requirement["mode"], python_requirement["param"]) == (
        "columns",
        "input_columns",
    )
    assert "accepted_column_types" not in python_requirement
    assert research_requirement["accepted_column_types"] == list(RICH_COLUMN_TYPES)
    assert research_requirement["accepted_cell_kinds"] == [
        "text",
        "blob",
        "template",
    ]


def test_research_descriptor_types_are_enforced_by_its_custom_resolver(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "research-source.frisket", name="Research")
    try:
        sheet_id = project.add_sheet("Inputs")
        project.add_column(sheet_id, "recording", type="audio")
        from frisket.engine.executor.actions import run_action_spec

        result = run_action_spec(
            project,
            {
                "action_id": "research.answer",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
                "params": {
                    "source": ["recording"],
                    "question": {"text": "What happened?"},
                    "model": "anthropic/claude-haiku-4-5",
                },
                "idempotency_key": "research-invalid-source",
            },
            project_id="source",
        )
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_input_ref"
        assert project.db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    finally:
        project.close()
