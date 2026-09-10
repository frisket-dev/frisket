from dataclasses import FrozenInstanceError

import pytest

from frisket.contracts.actions.source_inputs import resolve_source_columns
from frisket.sdk.inputs import (
    Column,
    Columns,
    Fields,
    FixedColumn,
    InputSelectionError,
    OneOf,
    RequiredAiInput,
    Template,
)


def test_fixed_column_projects_sheet_requirement_without_owning_a_param():
    source = FixedColumn(
        "enclosure_url",
        types=("link", "text"),
        label="Enclosure URL column",
        cell_kinds=("text",),
        message="Requires the enclosure_url column created by RSS import.",
    )

    selected = source.select({"sheet_id": 12})

    assert selected.names() == ("enclosure_url",)
    assert selected.source.accepted_types == ("link", "text")
    assert selected.field() == "params.sheet_id"
    assert source.owned_params() == frozenset()
    assert source.catalog_requirements() == [
        {
            "id": "enclosure_url",
            "mode": "fixed_column",
            "column_name": "enclosure_url",
            "label": "Enclosure URL column",
            "min": 1,
            "max": 1,
            "accepted_column_types": ["link", "text"],
            "accepted_cell_kinds": ["text"],
            "message": "Requires the enclosure_url column created by RSS import.",
        }
    ]


def test_column_is_frozen_and_projects_ordered_contract_metadata():
    source = Column(
        "source_column",
        types=("category", "text"),
        label="Address column",
        cell_kinds=("text",),
        message="Choose an address.",
        next_steps=("clean",),
    )

    assert source.select({"source_column": "address"}).names() == ("address",)
    assert source.catalog_requirements() == [
        {
            "id": "source_column",
            "mode": "column",
            "param": "source_column",
            "label": "Address column",
            "min": 1,
            "max": 1,
            "accepted_column_types": ["category", "text"],
            "accepted_cell_kinds": ["text"],
            "message": "Choose an address.",
            "next_steps": ["clean"],
        }
    ]
    with pytest.raises(FrozenInstanceError):
        source.param = "other"  # type: ignore[misc]


def test_columns_exact_template_derives_and_canonicalizes_token_order():
    source = Columns(
        "input_columns",
        types=("text", "category"),
        template_param="input_template",
        template_columns="exact",
    )

    omitted = source.select(
        {"input_columns": [], "input_template": "{{street}}, {{city}} {{street}}"}
    )
    reordered = source.select(
        {
            "input_columns": ["city", "street"],
            "input_template": "{{street}}, {{city}}",
        }
    )

    assert omitted.names() == ("street", "city")
    assert dict(omitted.params()) == {"input_columns": ["street", "city"]}
    assert reordered.names() == omitted.names()
    assert dict(reordered.params()) == dict(omitted.params())


@pytest.mark.parametrize(
    "columns",
    [
        ["street"],
        ["street", "city", "country"],
        ["street", "city", "street"],
    ],
)
def test_columns_exact_template_rejects_nonmatching_or_duplicate_members(columns):
    source = Columns(
        "input_columns",
        template_param="input_template",
        template_columns="exact",
    )

    with pytest.raises(InputSelectionError) as exc:
        source.select(
            {"input_columns": columns, "input_template": "{{street}}, {{city}}"}
        )

    assert exc.value.field == "params.input_columns"


def test_columns_union_template_preserves_authored_order_then_adds_references():
    source = Columns(
        "input_columns",
        template_param="input_template",
        template_columns="union",
    )

    selected = source.select(
        {
            "input_columns": ["photo", "city"],
            "input_template": "A view of {{city}} in {{country}}",
        }
    )

    assert selected.names() == ("photo", "city", "country")
    assert dict(selected.params()) == {"input_columns": ["photo", "city", "country"]}


def test_columns_union_params_carry_required_ai_policy_in_the_selection():
    source = Columns(
        "input_columns",
        required_ai_params=("judged_column",),
    )

    selected = source.select(
        {"input_columns": ["story"], "judged_column": "risk_score"}
    )

    assert selected.names() == ("story", "risk_score")
    assert selected.leaves()[0][1].columns == selected.names()
    assert selected.leaves()[0][1].required_ai_inputs == (
        RequiredAiInput("risk_score", "params.judged_column"),
    )
    assert selected.params() == {"input_columns": ["story", "risk_score"]}
    assert source.owned_params() == {"input_columns", "judged_column"}
    assert source.catalog_requirements()[0]["source_union_params"] == ["judged_column"]
    assert "required_ai_params" not in source.catalog_requirements()[0]
    with pytest.raises(InputSelectionError) as exc:
        source.select({"input_columns": ["story"]})
    assert exc.value.field == "params.judged_column"
    with pytest.raises(InputSelectionError, match="cardinality"):
        source.select({"input_columns": [], "judged_column": "risk_score"})


def test_template_without_columns_param_uses_only_template_references():
    source = Template(
        "template",
        types=("text", "number"),
        label="Template inputs",
    )

    selected = source.select({"template": "{{name}}: {{amount}}"})

    assert selected.names() == ("name", "amount")
    assert dict(selected.params()) == {}
    assert source.catalog_requirements() == [
        {
            "id": "template",
            "mode": "template",
            "param": "template",
            "label": "Template inputs",
            "min": 1,
            "accepted_column_types": ["text", "number"],
            "template_columns": "union",
        }
    ]


def test_one_of_is_strict_and_projects_one_flat_group():
    source = OneOf(
        "address",
        Column(
            "source_column",
            types=("category", "text"),
            label="Address column",
        ),
        Template(
            "input_template",
            columns="input_columns",
            types=("category", "date", "integer", "link", "number", "text"),
            template_columns="exact",
            label="Address parts",
        ),
        default="source_column",
    )

    assert source.select({"source_column": "address"}).names() == ("address",)
    with pytest.raises(InputSelectionError):
        source.select({})
    with pytest.raises(InputSelectionError) as exc:
        source.select(
            {
                "source_column": "address",
                "input_columns": ["city"],
                "input_template": "{{city}}",
            }
        )
    assert exc.value.field == "params.input_template"

    requirements = source.catalog_requirements()
    assert [item["mode"] for item in requirements] == ["column", "template"]
    assert [item["one_of_group"] for item in requirements] == [
        "address",
        "address",
    ]
    assert requirements[0]["one_of_default"] is True
    assert "one_of_default" not in requirements[1]
    assert requirements[1]["param"] == "input_columns"
    assert requirements[1]["template_param"] == "input_template"
    assert requirements[1]["template_columns"] == "exact"


def test_fields_project_per_role_types_and_can_require_distinct_columns():
    source = Fields(
        latitude=Column(
            "latitude_column", types=("number", "integer"), label="Latitude"
        ),
        longitude=Column(
            "longitude_column", types=("number", "integer"), label="Longitude"
        ),
        distinct=True,
    )

    selected = source.select({"latitude_column": "lat", "longitude_column": "lon"})
    assert [(role, leaf.columns[0]) for role, leaf in selected.leaves()] == [
        ("latitude", "lat"),
        ("longitude", "lon"),
    ]
    assert [
        (leaf.columns, leaf.source.accepted_types, leaf.source.error_field)
        for _, leaf in selected.leaves()
    ] == [
        (("lat",), ("number", "integer"), "params.latitude_column"),
        (("lon",), ("number", "integer"), "params.longitude_column"),
    ]
    assert [item["role"] for item in source.catalog_requirements()] == [
        "latitude",
        "longitude",
    ]
    with pytest.raises(InputSelectionError) as exc:
        source.select({"latitude_column": "same", "longitude_column": "same"})
    assert exc.value.source == "longitude"
    assert exc.value.field == "params.longitude_column"


def test_role_aware_resolution_can_bind_one_role_to_another_sheet():
    class Project:
        @staticmethod
        def columns(sheet_id):
            return [
                {
                    "id": sheet_id * 10,
                    "name": "shared_name",
                    "type": "text" if sheet_id == 1 else "category",
                }
            ]

    source = Fields(
        source=Column("source_column"),
        target=Column("target_column", sheet_param="target_sheet_id"),
    )
    assert source.owned_params() == {
        "source_column",
        "target_column",
        "target_sheet_id",
    }
    selected = source.select(
        {"source_column": "shared_name", "target_column": "shared_name"}
    )

    resolved = resolve_source_columns(
        Project(),
        sheet_id=1,
        selected=selected,
        params={"target_sheet_id": 2},
    )

    assert resolved.by_source == {"source": 10, "target": 20}
    assert resolved.column_types_by_source == {
        "source": "text",
        "target": "category",
    }


def test_distinct_fields_compare_actual_sheet_identity_not_parameter_names():
    source = Fields(
        left=Column("left_column", sheet_param="left_sheet_id"),
        right=Column("right_column", sheet_param="right_sheet_id"),
        distinct=True,
    )

    with pytest.raises(InputSelectionError, match="distinct"):
        source.select(
            {
                "left_column": "name",
                "right_column": "name",
                "left_sheet_id": 1,
                "right_sheet_id": 1,
            }
        )

    selected = source.select(
        {
            "left_column": "name",
            "right_column": "name",
            "left_sheet_id": 1,
            "right_sheet_id": 2,
        }
    )
    assert selected.names() == ("name",)


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [(-1, None), (2, 1), (True, None), (0, False)],
)
def test_invalid_cardinality_is_rejected_at_declaration(minimum, maximum):
    with pytest.raises(ValueError):
        Columns("input_columns", min=minimum, max=maximum)


def test_descriptor_sequences_reject_accidental_bare_strings():
    with pytest.raises(ValueError):
        Column("source_column", types="text")
    with pytest.raises(ValueError):
        Column("source_column", types={"text", "number"})

    with pytest.raises(InputSelectionError) as exc:
        Columns("input_columns").select({"input_columns": "title"})

    assert exc.value.field == "params.input_columns"


def test_one_of_ignores_blank_alternative_values():
    source = OneOf(
        "source",
        Column("source_column"),
        Template("input_template"),
    )

    selected = source.select({"source_column": "  ", "input_template": "{{title}}"})

    assert selected.names() == ("title",)
