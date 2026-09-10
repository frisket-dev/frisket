from types import SimpleNamespace

from frisket.contracts.action import ActionError
from frisket.contracts.actions.source_inputs import _all_of
from frisket.engine.store.project import Project
from frisket.sdk.inputs import Column, Columns, Fields, OneOf, Template
from frisket.sdk.maprunner import build_resolve_fn, build_runner_spec_fn


def _decl(inputs, **changes):
    values = {
        "kind": "test.source_descriptor",
        "inputs": inputs,
        "passthrough": (),
        "runner_spec_extra": None,
        "runner_spec_resolve_extra": None,
        "omit_empty_input_columns": False,
        "rich_input_columns": False,
        "output_name_attr": "output_name",
        "row_ids_attr": "row_ids",
        "multi_output": False,
        "row_scope_policy": {
            "kind": "sheet_rows",
            "selectors": ["all_rows", "exact_membership"],
        },
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_runner_spec_uses_descriptor_order_and_canonical_params() -> None:
    decl = _decl(
        Columns(
            "input_columns",
            template_param="input_template",
            template_columns="exact",
        ),
        passthrough=(("input_template", "truthy"),),
        # A transitional hook must not be able to override descriptor identity.
        runner_spec_extra=lambda _params: {"input_columns": ["obsolete"]},
    )
    params = SimpleNamespace(
        sheet_id=4,
        input_columns=["city", "street"],
        input_template="{{street}}, {{city}} / {{street}}",
    )

    spec = build_runner_spec_fn(decl)(params)

    assert spec == {
        "action_kind": "test.source_descriptor",
        "sheet_id": 4,
        "input_columns": ["street", "city"],
        "input_template": "{{street}}, {{city}} / {{street}}",
    }


def test_descriptor_type_gate_refuses_wrong_column_type(tmp_path) -> None:
    decl = _decl(Column("source_column", types=("text",)))
    project = Project.create(tmp_path / "descriptor-types.frisket", name="types")
    try:
        sheet_id = project.add_sheet("Data")
        number_id = project.add_column(sheet_id, "count", type="number")
        project.add_rows(sheet_id, [{"count": 2}], {"count": number_id})
        params = SimpleNamespace(sheet_id=sheet_id, source_column="count")

        result = build_resolve_fn(decl)(
            project, params, build_runner_spec_fn(decl)(params)
        )

        assert isinstance(result, ActionError)
        assert result.code == "invalid_input_ref"
        assert result.field == "params.source_column"
        assert result.details == {
            "columns": [{"name": "count", "type": "number"}],
            "accepted_column_types": ["text"],
        }
    finally:
        project.close()


def test_combined_columns_preserve_names_and_each_parameters_type(tmp_path) -> None:
    decl = _decl(
        _all_of(Column("name", types=("text",)), Column("age", types=("integer",))),
        passthrough=(("name", "always"), ("age", "always")),
    )
    project = Project.create(tmp_path / "heterogeneous.frisket", name="Types")
    try:
        sheet_id = project.add_sheet("People")
        name_id = project.add_column(sheet_id, "full_name", type="text")
        age_id = project.add_column(sheet_id, "years", type="integer")
        project.add_rows(
            sheet_id,
            [{"full_name": "Ada", "years": 36}],
            {"full_name": name_id, "years": age_id},
        )
        valid = SimpleNamespace(sheet_id=sheet_id, name="full_name", age="years")
        selected = decl.inputs.select(valid)
        spec = build_runner_spec_fn(decl)(valid)
        assert selected.names() == ("full_name", "years")
        assert selected.source.accepted_types is None
        assert [
            (leaf.columns, leaf.source.accepted_types, leaf.source.error_field)
            for _, leaf in selected.leaves()
        ] == [
            (("full_name",), ("text",), "params.name"),
            (("years",), ("integer",), "params.age"),
        ]
        assert spec["input_columns"] == ["full_name", "years"]
        assert spec["name"] == "full_name" and spec["age"] == "years"
        assert [
            (row["mode"], row["param"]) for row in decl.inputs.catalog_requirements()
        ] == [("column", "name"), ("column", "age")]
        resolved = build_resolve_fn(decl)(project, valid, spec)
        assert not isinstance(resolved, ActionError)
        assert resolved["input_column_ids"] == {"full_name": name_id, "years": age_id}
        assert resolved["input_column_types"] == {
            "full_name": "text",
            "years": "integer",
        }

        for name, age, field, column, accepted in (
            (
                "years",
                "years",
                "params.name",
                {"name": "years", "type": "integer"},
                ["text"],
            ),
            (
                "full_name",
                "full_name",
                "params.age",
                {"name": "full_name", "type": "text"},
                ["integer"],
            ),
        ):
            wrong = SimpleNamespace(sheet_id=sheet_id, name=name, age=age)
            refused = build_resolve_fn(decl)(
                project, wrong, build_runner_spec_fn(decl)(wrong)
            )
            assert isinstance(refused, ActionError)
            assert refused.code == "invalid_input_ref"
            assert refused.field == field
            assert refused.details == {
                "columns": [column],
                "accepted_column_types": accepted,
            }

        missing = SimpleNamespace(sheet_id=sheet_id, name="full_name", age="missing")
        refused = build_resolve_fn(decl)(
            project, missing, build_runner_spec_fn(decl)(missing)
        )
        assert isinstance(refused, ActionError)
        assert refused.field == "params.age"
        assert refused.details == {"missing": ["missing"]}
    finally:
        project.close()


def test_resolver_does_not_fall_back_to_params_for_row_scope(tmp_path) -> None:
    decl = _decl(Column("source_column"))
    project = Project.create(tmp_path / "runner-scope.frisket", name="scope")
    try:
        sheet_id = project.add_sheet("Data")
        project.add_column(sheet_id, "source", type="text")
        params = SimpleNamespace(sheet_id=sheet_id, source_column="source")

        result = build_resolve_fn(decl)(project, params, {})

        assert isinstance(result, ActionError)
        assert result.code == "invalid_input_ref"
        assert result.field == "row_scope.sheet_id"
    finally:
        project.close()


def test_fields_resolve_by_role_with_each_roles_own_type_contract(tmp_path) -> None:
    decl = _decl(
        Fields(
            latitude=Column("latitude_column", types=("number", "integer")),
            longitude=Column("longitude_column", types=("number", "integer")),
        ),
        passthrough=(
            ("latitude_column", "always"),
            ("longitude_column", "always"),
        ),
        omit_empty_input_columns=True,
    )
    project = Project.create(tmp_path / "descriptor-fields.frisket", name="fields")
    try:
        sheet_id = project.add_sheet("Data")
        lat_id = project.add_column(sheet_id, "lat", type="number")
        lon_id = project.add_column(sheet_id, "lon", type="integer")
        project.add_rows(
            sheet_id,
            [{"lat": 1.5, "lon": 2}],
            {"lat": lat_id, "lon": lon_id},
        )
        params = SimpleNamespace(
            sheet_id=sheet_id,
            latitude_column="lat",
            longitude_column="lon",
        )

        runner_spec = build_runner_spec_fn(decl)(params)
        resolved = build_resolve_fn(decl)(project, params, runner_spec)

        assert "input_columns" not in runner_spec
        assert not isinstance(resolved, ActionError)
        assert resolved["input_column_ids"] == {
            "latitude": lat_id,
            "longitude": lon_id,
        }
        assert resolved["input_column_types"] == {
            "latitude": "number",
            "longitude": "integer",
        }
    finally:
        project.close()


def test_fields_resolve_roles_from_their_declared_sheets(tmp_path) -> None:
    decl = _decl(
        Fields(
            source=Column("source_column"),
            target=Column("target_column", sheet_param="target_sheet_id"),
        )
    )
    project = Project.create(tmp_path / "descriptor-sheets.frisket", name="sheets")
    try:
        source_sheet_id = project.add_sheet("Source")
        target_sheet_id = project.add_sheet("Target")
        source_id = project.add_column(source_sheet_id, "name", type="text")
        target_id = project.add_column(target_sheet_id, "name", type="category")
        params = SimpleNamespace(
            sheet_id=source_sheet_id,
            target_sheet_id=target_sheet_id,
            source_column="name",
            target_column="name",
        )

        resolved = build_resolve_fn(decl)(
            project, params, build_runner_spec_fn(decl)(params)
        )

        assert not isinstance(resolved, ActionError)
        assert resolved["input_column_ids"] == {
            "source": source_id,
            "target": target_id,
        }
        assert resolved["input_column_types"] == {
            "source": "text",
            "target": "category",
        }
    finally:
        project.close()


def test_descriptor_selection_error_is_an_invalid_input_ref(tmp_path) -> None:
    decl = _decl(
        OneOf(
            "source",
            Column("source_column"),
            Template("input_template", columns="input_columns"),
        )
    )
    project = Project.create(tmp_path / "descriptor-invalid.frisket", name="invalid")
    try:
        sheet_id = project.add_sheet("Data")
        params = SimpleNamespace(
            sheet_id=sheet_id,
            source_column=None,
            input_template=None,
            input_columns=[],
        )

        result = build_resolve_fn(decl)(
            project,
            params,
            {"action_kind": decl.kind, "sheet_id": sheet_id},
        )

        assert isinstance(result, ActionError)
        assert result.code == "invalid_input_ref"
        assert result.field == "params"
        assert result.details == {
            "reason": "exactly one source alternative must be active"
        }
    finally:
        project.close()
