from dataclasses import FrozenInstanceError, replace

import pytest

from frisket.actions.core import (
    Action,
    ActionCategory,
    RegisteredAction,
    RowScope,
    action,
)
from frisket.actions.exports import SHEET_CSV
from frisket.actions.query import preview_query


def test_shared_project_capability_does_not_choose_action_presentation():
    default = action(
        name="default_preview",
        title="Default preview",
        description="Preview the selected query.",
        category=ActionCategory.SOURCES,
        run=preview_query,
    )
    specialized = action(
        name="specialized_preview",
        title="Specialized preview",
        description="Preview the selected query.",
        category=ActionCategory.SOURCES,
        run=preview_query,
        form="query_preview",
    )
    assert default.run.single_spec() is specialized.run.single_spec()
    assert default.form == "generated"
    assert specialized.form == "query_preview"
    assert (
        RegisteredAction("test.default_preview", default).catalog_entry()["ui_hints"][
            "form"
        ]
        == "generated"
    )
    assert (
        RegisteredAction("test.specialized_preview", specialized).catalog_entry()[
            "ui_hints"
        ]["form"]
        == "query_preview"
    )
    with pytest.raises(FrozenInstanceError):
        default.form = "changed"


@pytest.mark.parametrize("form", [None, 3, True, "", " ", " generated", "generated "])
def test_action_forms_must_be_nonempty_trimmed_strings(form):
    with pytest.raises(ValueError, match="action form"):
        action(
            name="preview",
            title="Preview",
            description="Preview a query.",
            category=ActionCategory.SOURCES,
            run=preview_query,
            form=form,
        )
    with pytest.raises(ValueError, match="action form"):
        replace(SHEET_CSV, form=form)


def test_direct_action_constructor_defaults_to_generated():
    definition = Action(
        name="export",
        title="Export",
        description="Export a sheet.",
        category=ActionCategory.CONVERT,
        run=SHEET_CSV.run,
        row_scope=RowScope.SELECTABLE_ROWS,
        export_target=SHEET_CSV.export_target,
    )
    assert definition.form == "generated"
    hints = RegisteredAction("test.export", definition).catalog_entry()["ui_hints"]
    assert hints["form"] == "generated"
    assert hints["export_target"]["form"] == "generated"


def test_export_target_form_comes_from_the_action_declaration():
    definition = replace(SHEET_CSV, form="alternate_csv_export")
    hints = RegisteredAction("test.export", definition).catalog_entry()["ui_hints"]
    assert hints["form"] == "alternate_csv_export"
    assert hints["export_target"]["form"] == "alternate_csv_export"
