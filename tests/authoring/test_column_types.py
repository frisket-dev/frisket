"""Pluggable column-type registry and plugin contracts.

Backend contract: column types are a REGISTRY (frisket.column_types), not an
enum. Core types live on the same seam plugins use; the registry is exposed
over GET /api/column-types with presentation hints; a registered type is
usable on real columns through store and the v1 column.set_type action. Retyping
preserves incompatible values and marks them invalid; the op lands in history
and undoes/redoes. The frontend half (renderers resolved from the registry)
lives in web/src/grid/typeRegistry.ts + cells.tsx.
"""

import pytest
from fastapi.testclient import TestClient

from helpers import make_client
from frisket.authoring import column_types
from frisket.ai.llm import ModelRouter
from frisket.engine.store import COLUMN_TYPES, Project
from http_test_helpers import (
    post_cell_edit_as_v1_action,
    post_column_set_type_as_v1_action,
    post_operation_redo_as_v1_action,
    post_operation_undo_as_v1_action,
)
from workbench_runtime_test_helpers import activate_runtime_plugin_for_project


PLUGIN_TYPE_OWNER = "test.column_types"


@pytest.fixture
def client(tmp_path) -> TestClient:
    return make_client(tmp_path, router=ModelRouter(cache=None, cache_mode="off"))


@pytest.fixture
def plugin_type():
    """A throwaway plugin type, removed after the test."""
    name = "obj_envelope"
    column_types.register_column_type(
        name,
        validate=lambda v: isinstance(v, (dict, str)),
        presentation={"renderer": "json", "badge": "obj"},
        description="test plugin type",
        plugin=PLUGIN_TYPE_OWNER,
    )
    yield name
    column_types.unregister_column_type(name)


def _import(client, pid: str, csv: str) -> int:
    r = client.post(
        f"/api/projects/{pid}/import/csv", files={"file": ("t.csv", csv, "text/csv")}
    )
    assert r.status_code == 200, r.text
    return r.json()["sheet_id"]


def _enable_plugin_for_project(
    client: TestClient,
    pid: str,
    *,
    plugin_id: str = "external",
    column_type: str,
) -> None:
    project = client.app.state.workspace.get(pid)
    # Install through the real seam: project enablement alone is intentionally
    # insufficient after package identity moved to the workspace catalog.
    activate_runtime_plugin_for_project(
        project,
        client.app.state.workspace.root / "test-plugin-packages",
        plugin_id=plugin_id,
        runtime_bindings={},
        project_id=pid,
        column_types=[column_type],
    )


# ---------------------------------------------------------------------------
# registry semantics


class TestRegistry:
    def test_core_types_registered_on_seam(self):
        for name in (
            "text",
            "number",
            "integer",
            "boolean",
            "category",
            "json",
            "date",
            "image",
            "audio",
            "video",
            "file",
            "link",
        ):
            spec = column_types.get_column_type(name)
            assert spec is not None, f"core type {name} missing"
            assert spec.core
            assert "renderer" in spec.presentation

    def test_plugin_register_and_unregister(self, plugin_type):
        spec = column_types.get_column_type(plugin_type)
        assert spec is not None and not spec.core
        assert spec.plugin == PLUGIN_TYPE_OWNER
        assert spec.presentation["renderer"] == "json"
        assert column_types.is_registered(plugin_type)

    def test_plugin_cannot_shadow_core_type(self):
        with pytest.raises(ValueError, match="core"):
            column_types.register_column_type("text", presentation={"renderer": "evil"})
        with pytest.raises(ValueError, match="core"):
            column_types.unregister_column_type("text")

    def test_plugin_cannot_override_reserved_semantic_type(self):
        spec = column_types.get_column_type("geo_point")
        assert spec is not None and not spec.core
        with pytest.raises(ValueError, match="reserved"):
            column_types.register_column_type(
                "geo_point",
                validate=lambda value: isinstance(value, str),
                presentation={"renderer": "evil"},
            )
        with pytest.raises(ValueError, match="reserved"):
            column_types.unregister_column_type("geo_point")
        assert column_types.get_column_type("geo_point") == spec

    def test_validate_value(self, plugin_type):
        assert column_types.validate_value(plugin_type, {"a": 1})
        assert not column_types.validate_value(plugin_type, 7)
        # None (an empty cell) always passes; unknown types accept anything
        assert column_types.validate_value(plugin_type, None)
        assert column_types.validate_value("no_such_type", object())

    def test_core_validators(self):
        assert column_types.validate_value("integer", -(2**63))
        assert column_types.validate_value("integer", 2**63 - 1)
        assert not column_types.validate_value("integer", -(2**63) - 1)
        assert not column_types.validate_value("integer", 2**63)
        assert not column_types.validate_value("integer", 3.0)
        assert not column_types.validate_value("integer", True)
        assert not column_types.validate_value("integer", "three")
        assert not column_types.validate_value("boolean", "true")
        assert column_types.validate_value("boolean", True)
        assert not column_types.validate_value("number", float("inf"))
        assert not column_types.validate_value("number", float("nan"))
        assert not column_types.validate_value("json", {"value": float("nan")})

    def test_range_facet_capability_is_closed_and_registry_derived(self):
        name = "plugin_number_facet"
        column_types.register_column_type(
            name,
            validate=lambda value: isinstance(value, (int, float)),
            presentation={
                "renderer": "number",
                "facet": {"kind": "range", "valueKind": "number"},
            },
            plugin=PLUGIN_TYPE_OWNER,
        )
        try:
            assert column_types.range_facet_value_kind(name) == "number"
            assert column_types.range_facet_value_kind("integer") == "integer"
            assert column_types.range_facet_value_kind("text") is None
        finally:
            column_types.unregister_column_type(name)

    def test_category_declares_exact_value_facet_behavior(self):
        spec = column_types.get_column_type("category")
        assert spec is not None
        assert spec.presentation == {
            "renderer": "category",
            "facet": {
                "kind": "categorical",
                "preferred": True,
                "oneClick": True,
                "operator": "eq",
            },
        }
        assert "clickable exact-value facet" in spec.description

    def test_legacy_enum_view_is_live(self, plugin_type):
        # the old `t in COLUMN_TYPES` contract still works AND sees plugins
        assert "text" in COLUMN_TYPES
        assert plugin_type in COLUMN_TYPES
        assert "no_such_type" not in COLUMN_TYPES
        assert len(COLUMN_TYPES) >= 12


# ---------------------------------------------------------------------------
# store integration


class TestStore:
    def test_add_column_accepts_plugin_type(self, tmp_path, plugin_type):
        p = Project.create(tmp_path / "t.frisket", name="t")
        sheet = p.add_sheet("data")
        cid = p.add_column(sheet, "payload", type=plugin_type)
        assert p.get_column(cid)["type"] == plugin_type

    def test_add_column_rejects_unknown_type(self, tmp_path):
        p = Project.create(tmp_path / "t.frisket", name="t")
        sheet = p.add_sheet("data")
        with pytest.raises(ValueError, match="unknown column type"):
            p.add_column(sheet, "payload", type="no_such_type")

    def test_set_column_type(self, tmp_path, plugin_type):
        p = Project.create(tmp_path / "t.frisket", name="t")
        sheet = p.add_sheet("data")
        cid = p.add_column(sheet, "payload", type="text")
        p.set_column_type(cid, plugin_type)
        assert p.get_column(cid)["type"] == plugin_type
        with pytest.raises(ValueError):
            p.set_column_type(cid, "no_such_type")

    @pytest.mark.parametrize("value", [-(2**63) - 1, 2**63, 1.0, True])
    def test_integer_storage_preserves_but_excludes_invalid_values(
        self, tmp_path, value
    ):
        p = Project.create(tmp_path / "integer-storage.frisket", name="t")
        sheet = p.add_sheet("data")
        cid = p.add_column(sheet, "value", type="integer")
        first = p.add_rows(sheet, [{"value": value}], {"value": cid})[0]
        second = p.add_row_with_undo(sheet, {"value": value}, {"value": cid})
        assert p.get_values(sheet, cid) == {first: None, second: None}
        assert p.get_values(sheet, cid, preserve_invalid=True) == {
            first: value,
            second: value,
        }

    def test_retype_to_integer_marks_out_of_range_existing_data_invalid(self, tmp_path):
        p = Project.create(tmp_path / "integer-retype.frisket", name="t")
        sheet = p.add_sheet("data")
        cid = p.add_column(sheet, "value", type="json")
        p.add_rows(sheet, [{"value": 2**63}], {"value": cid})
        p.set_column_type(cid, "integer")
        assert p.get_column(cid)["type"] == "integer"
        assert p.get_values(sheet, cid) == {1: None}
        assert p.get_values(sheet, cid, preserve_invalid=True) == {1: 2**63}

    def test_hidden_column_revival_reclassifies_retained_data(self, tmp_path):
        p = Project.create(tmp_path / "integer-revival.frisket", name="t")
        sheet = p.add_sheet("data")
        cid = p.add_column(sheet, "value", type="json")
        p.add_rows(sheet, [{"value": 2**63}], {"value": cid})
        p.db.execute("UPDATE columns SET hidden=1 WHERE id=?", (cid,))
        p.db.commit()

        assert p.add_column(sheet, "value", type="integer") == cid

        retained = p.get_column(cid)
        assert retained["type"] == "integer"
        assert retained["hidden"] == 0
        assert p.get_values(sheet, cid) == {1: None}
        assert p.get_values(sheet, cid, preserve_invalid=True) == {1: 2**63}


# ---------------------------------------------------------------------------
# API integration


class TestApi:
    def test_unscoped_endpoint_exposes_core_and_builtin_semantic_types_only(
        self, client, plugin_type
    ):
        r = client.get("/api/column-types")
        assert r.status_code == 200
        types = {t["name"]: t for t in r.json()}
        assert {"text", "integer", "image", "link", "geo_point"} <= set(types)
        assert types["text"]["core"] is True
        assert plugin_type not in types
        # presentation hints carry through verbatim (frontend resolves these)
        assert types["audio"]["presentation"] == {
            "renderer": "media",
            "mediaType": "audio",
        }
        assert types["category"]["presentation"] == {
            "renderer": "category",
            "facet": {
                "kind": "categorical",
                "preferred": True,
                "oneClick": True,
                "operator": "eq",
            },
        }
        assert types["json"]["presentation"] == {
            "renderer": "json",
            "facet": {"kind": "collection", "operator": "list_contains_any"},
        }
        assert types["geo_point"]["presentation"]["renderer"] == "map-pin"

    def test_project_endpoint_exposes_enabled_plugin_type(self, client, plugin_type):
        pid = client.post("/api/projects", json={"name": "T"}).json()["id"]
        _enable_plugin_for_project(
            client,
            pid,
            plugin_id=PLUGIN_TYPE_OWNER,
            column_type=plugin_type,
        )
        r = client.get(f"/api/projects/{pid}/column-types")
        assert r.status_code == 200
        types = {t["name"]: t for t in r.json()}
        assert types[plugin_type]["core"] is False
        assert types[plugin_type]["plugin"] == PLUGIN_TYPE_OWNER
        assert types[plugin_type]["presentation"]["renderer"] == "json"
        assert types[plugin_type]["has_validator"] is True

    def test_patch_column_to_plugin_type(self, client, plugin_type):
        pid = client.post("/api/projects", json={"name": "T"}).json()["id"]
        _enable_plugin_for_project(
            client,
            pid,
            plugin_id=PLUGIN_TYPE_OWNER,
            column_type=plugin_type,
        )
        sheet_id = _import(client, pid, 'payload\n"{""a"": 1}"\n')
        col = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()[
            "columns"
        ][0]
        r = post_column_set_type_as_v1_action(client, pid, col["id"], plugin_type)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "completed"
        data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
        assert data["columns"][0]["type"] == plugin_type

    def test_integer_cell_edit_accepts_boundary_and_rejects_out_of_range(self, client):
        pid = client.post("/api/projects", json={"name": "Integer edit"}).json()["id"]
        sheet_id = _import(client, pid, "value\n1\n")
        data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
        column_id = int(data["columns"][0]["id"])
        row_id = int(data["rows"][0]["id"])
        accepted = post_cell_edit_as_v1_action(
            client,
            pid,
            [
                {
                    "row_id": row_id,
                    "column_id": column_id,
                    "value": 2**63 - 1,
                }
            ],
        )
        assert accepted.status_code == 200, accepted.text
        rejected = post_cell_edit_as_v1_action(
            client,
            pid,
            [
                {
                    "row_id": row_id,
                    "column_id": column_id,
                    "value": 2**63,
                }
            ],
        )
        assert rejected.status_code == 400, rejected.text
        assert rejected.json()["errors"][0]["code"] == "column_value_validation_failed"

    def test_patch_column_unknown_type_422(self, client):
        pid = client.post("/api/projects", json={"name": "T"}).json()["id"]
        sheet_id = _import(client, pid, "a\nx\n")
        col = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()[
            "columns"
        ][0]
        r = post_column_set_type_as_v1_action(client, pid, col["id"], "no_such_type")
        assert r.status_code == 400
        assert r.json()["errors"][0]["code"] == "invalid_column_type"

    def test_patch_preserves_and_marks_values_invalid_for_new_type(self, client):
        pid = client.post("/api/projects", json={"name": "T"}).json()["id"]
        sheet_id = _import(client, pid, "a\nhello\n")
        col = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()[
            "columns"
        ][0]
        r = post_column_set_type_as_v1_action(client, pid, col["id"], "boolean")
        assert r.status_code == 200, r.text
        data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
        assert data["rows"][0]["cells"][str(col["id"])] == "hello"
        assert data["rows"][0]["meta"][str(col["id"])]["invalid"] is True

    def test_retype_is_logged_and_undoable(self, client, plugin_type):
        pid = client.post("/api/projects", json={"name": "T"}).json()["id"]
        _enable_plugin_for_project(
            client,
            pid,
            plugin_id=PLUGIN_TYPE_OWNER,
            column_type=plugin_type,
        )
        sheet_id = _import(client, pid, "a\nhello\n")
        col = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()[
            "columns"
        ][0]
        r = post_column_set_type_as_v1_action(client, pid, col["id"], plugin_type)
        assert r.status_code == 200
        set_type_op_id = r.json()["op_ids"][0]

        hist = client.get(f"/api/projects/{pid}/history").json()["ops"]
        kinds = [o["kind"] for o in hist]
        assert "column.set_type" in kinds

        def coltype() -> str:
            data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
            return {c["id"]: c for c in data["columns"]}[col["id"]]["type"]

        assert coltype() == plugin_type
        undo = post_operation_undo_as_v1_action(
            client, pid, expected_op_id=set_type_op_id
        )
        assert undo.status_code == 200, undo.text
        assert undo.json()["schema_version"] == "frisket.action_result.v1"
        assert undo.json()["status"] == "completed"
        assert coltype() == "text"
        redo = post_operation_redo_as_v1_action(
            client, pid, expected_op_id=set_type_op_id
        )
        assert redo.status_code == 200, redo.text
        assert redo.json()["schema_version"] == "frisket.action_result.v1"
        assert redo.json()["status"] == "completed"
        assert coltype() == plugin_type


class TestDateValidator:
    """The date type validates ISO-8601, not 'any string' — retyping a column
    of arbitrary text onto `date` must be refusable (a validator that accepts
    everything validates nothing)."""

    @pytest.mark.parametrize(
        "ok",
        [
            "2026-06-12",
            "2026-06-12T14:30:00Z",
            "2026-06-12T14:30:00+02:00",
            None,
        ],
    )
    def test_accepts_iso_dates_and_empty(self, ok):
        assert column_types.validate_value("date", ok)

    @pytest.mark.parametrize(
        "bad",
        [
            "banana",
            "June 12, 2026",
            "12/06/2026",
            "2026-13-45",
            "2026-06-12T14:30:00",
            " 2026-06-12 ",
            "20260612",
            "2026-W24-5",
            "0",
            "",
            20260612,
        ],
    )
    def test_rejects_non_iso(self, bad):
        assert not column_types.validate_value("date", bad)
