from __future__ import annotations


import pytest

from frisket.engine.store.output_families import OutputFamilyStore


def test_family_creation_rolls_back_every_column_on_collision(make_project) -> None:
    project = make_project("family")
    sheet_id = project.add_sheet("Data")
    project.add_column(sheet_id, "source", type="text")

    with pytest.raises(ValueError, match="complete reusable generated family"):
        OutputFamilyStore(project).create_or_reuse(
            sheet_id=sheet_id,
            fields=[
                {"name": "meta_kind", "column_type": "text"},
                {"name": "source", "column_type": "json"},
            ],
        )

    assert [row["name"] for row in project.columns(sheet_id)] == ["source"]


def test_family_creation_and_exact_generated_reuse(
    make_project,
) -> None:
    project = make_project("family")
    sheet_id = project.add_sheet("Data")
    store = OutputFamilyStore(project)
    first, created = store.create_or_reuse(
        sheet_id=sheet_id,
        fields=[
            {
                "name": "meta_kind",
                "column_type": "text",
                "format": "markdown",
            },
            {"name": "meta_details", "column_type": "json"},
        ],
    )

    assert set(first) == {"meta_kind", "meta_details"}
    assert created == [first["meta_kind"], first["meta_details"]]

    second, created_again = store.create_or_reuse(
        sheet_id=sheet_id,
        fields=[
            {
                "name": "meta_kind",
                "column_type": "text",
                "format": "markdown",
            },
            {"name": "meta_details", "column_type": "json"},
        ],
    )

    assert second == first
    assert created_again == []
    reused = project.get_column(first["meta_kind"])
    assert reused["type"] == "text"
    assert reused["format"] == "markdown"


def test_family_reuse_rejects_hidden_generated_columns_without_reviving(
    make_project,
) -> None:
    project = make_project("family")
    sheet_id = project.add_sheet("Data")
    hidden_id = project.add_column(
        sheet_id,
        "meta_details",
        type="text",
        ai_generated=True,
        format="markdown",
        hidden=True,
    )

    with pytest.raises(ValueError, match="exact reusable generated family"):
        OutputFamilyStore(project).create_or_reuse(
            sheet_id=sheet_id,
            fields=[
                {
                    "name": "meta_details",
                    "column_type": "json",
                    "format": "filesize",
                    "default_hidden": True,
                }
            ],
        )

    row = project.get_column(hidden_id)
    assert row["hidden"] == 1
    assert row["default_hidden"] == 0
    assert row["type"] == "text"
    assert row["format"] == "markdown"
    assert row["current_run_id"] is None


def test_family_reuse_rejects_wrong_type_without_coercion(make_project) -> None:
    project = make_project("family")
    sheet_id = project.add_sheet("Data")
    column_id = project.add_column(
        sheet_id,
        "meta_kind",
        type="text",
        ai_generated=True,
        format="markdown",
    )

    with pytest.raises(ValueError, match="exact reusable generated family"):
        OutputFamilyStore(project).create_or_reuse(
            sheet_id=sheet_id,
            fields=[
                {
                    "name": "meta_kind",
                    "column_type": "category",
                    "format": None,
                }
            ],
        )

    row = project.get_column(column_id)
    assert row["type"] == "text"
    assert row["format"] == "markdown"


def test_family_validation_precedes_writes(make_project) -> None:
    project = make_project("family")
    sheet_id = project.add_sheet("Data")
    with pytest.raises(ValueError, match="must be unique"):
        OutputFamilyStore(project).create_or_reuse(
            sheet_id=sheet_id,
            fields=[
                {"name": "meta", "column_type": "json"},
                {"name": "meta", "column_type": "text"},
            ],
        )
    assert project.columns(sheet_id) == []
