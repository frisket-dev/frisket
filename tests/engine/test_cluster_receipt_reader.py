from __future__ import annotations

import json
from dataclasses import replace

import pytest

from frisket.actions.entity_types import ClusterReceiptSource
from frisket.actions.types import TableError
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.cluster_receipt_read import (
    AdmittedClusterReceiptReader,
    validate_cluster_receipt_fence,
)
from frisket.engine.store import Project


@pytest.fixture
def seeded(tmp_path):
    project = Project.create(tmp_path / "entities.frisket")
    sheet = project.add_sheet("People")
    column = project.add_column(sheet, "name")
    rows = project.add_rows(
        sheet,
        [
            {"name": name}
            for name in ("Jon Smith", "Smith, Jon", "Acme Corp", "Acme  Corp")
        ],
        {"name": column},
    )
    result = run_action_spec(
        project,
        {
            "action_id": "cluster.values",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {
                "source": "name",
                "method": "fingerprint",
                "min_size": 2,
            },
            "idempotency_key": "clusters",
        },
        project_id="p",
    )
    assert result.status == "completed", result.errors
    try:
        yield project, sheet, column, rows, result.receipt_id
    finally:
        project.close()


def _request(receipt_id, **params):
    return {
        "action_id": "resolve.entities",
        "scope": {"kind": "project"},
        "sheet_name": "Entities",
        "idempotency_key": "entities",
        "params": {
            "source": {"kind": "cluster_values", "receipt_id": receipt_id},
            **params,
        },
        "output_names": {"entity": "Canonical", "source_variants": "Sources"},
    }


def test_reader_snapshot_is_read_only_and_fence_rechecks_source(seeded):
    project, sheet, column, rows, receipt_id = seeded
    before = tuple(project.db.iterdump())
    reader = AdmittedClusterReceiptReader(project)
    groups = reader.read(
        ClusterReceiptSource(kind="cluster_values", receipt_id=receipt_id)
    )
    assert len(groups) == 2 and reader.parent_sheet_id == sheet
    assert {token.row_id for token in reader.sources} == set(rows)
    assert all(token in reader.sources for group in groups for token in group.sources)
    assert tuple(project.db.iterdump()) == before
    validate_cluster_receipt_fence(project, reader.facts[0])
    project.apply_edits(
        [{"row_id": rows[0], "column_id": column, "value": "different"}]
    )
    with pytest.raises(TableError, match="changed"):
        validate_cluster_receipt_fence(project, reader.facts[0])


def test_reader_refuses_changed_published_canonical_value(seeded):
    project, _, _, rows, receipt_id = seeded
    body = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()[0]
    )
    canonical = next(
        item["ref"]
        for item in body["outputs"]
        if item["ref"].get("kind") == "map_result_column"
    )
    project.apply_edits(
        [
            {
                "row_id": rows[0],
                "column_id": canonical["column_id"],
                "value": "Not the reviewed canonical",
            }
        ]
    )
    with pytest.raises(TableError) as error:
        AdmittedClusterReceiptReader(project).read(
            ClusterReceiptSource(kind="cluster_values", receipt_id=receipt_id)
        )
    assert error.value.code == "stale_replay"


def test_reader_refuses_groups_diverging_from_published_canonical_values(seeded):
    project, _, _, _, receipt_id = seeded
    body = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()[0]
    )
    for section in ("inputs", "outputs", "evidence"):
        for item in body[section]:
            ref = item["ref"]
            if ref.get("kind") == "value_clusters":
                for group in ref["clusters"]:
                    group["canonical"] = "A different claimed canonical"
    project.db.execute(
        "UPDATE receipts SET body=? WHERE id=?", (json.dumps(body), receipt_id)
    )
    project.db.commit()
    with pytest.raises(TableError) as error:
        AdmittedClusterReceiptReader(project).read(
            ClusterReceiptSource(kind="cluster_values", receipt_id=receipt_id)
        )
    assert error.value.code == "stale_replay"


def test_custom_registered_clusterer_receipt_is_consumable(seeded, monkeypatch):
    from frisket.actions.core import RegisteredAction
    from frisket.actions.registry import ACTION_REGISTRY

    project, sheet, _, _, _ = seeded
    definition = replace(
        ACTION_REGISTRY.get("cluster.values").definition, name="canonicalize"
    )
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {
            **ACTION_REGISTRY._actions,
            "custom.canonicalize": RegisteredAction("custom.canonicalize", definition),
        },
    )
    clustered = run_action_spec(
        project,
        {
            "action_id": "custom.canonicalize",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "output_names": {"canonical": "Custom canonical"},
            "params": {"source": "name"},
            "idempotency_key": "custom-clusters",
        },
        project_id="p",
    )
    assert clustered.status == "completed", clustered.errors
    result = run_action_spec(project, _request(clustered.receipt_id), project_id="p")
    assert result.status == "completed", result.errors
    assert len(project.visible_row_ids(result.outputs[0].sheet_id)) == 2


@pytest.mark.parametrize("change", ["missing_facts", "wrong_producer"])
def test_reader_requires_actual_persisted_capability_facts(seeded, change):
    project, _, _, _, receipt_id = seeded
    receipt = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()[0]
    )
    if change == "missing_facts":
        project.db.execute(
            "UPDATE ops SET spec=json_remove(spec,'$.value_clusters_result') "
            "WHERE id=(SELECT op_id FROM runs WHERE id=?)",
            (receipt["run_id"],),
        )
    else:
        project.db.execute(
            "UPDATE runs SET action_kind='map.template' WHERE id=?",
            (receipt["run_id"],),
        )
    project.db.commit()
    with pytest.raises(TableError) as error:
        AdmittedClusterReceiptReader(project).read(
            ClusterReceiptSource(kind="cluster_values", receipt_id=receipt_id)
        )
    assert error.value.code == "stale_replay"


def test_order_overrides_renames_and_replay_bind_all_outputs(seeded):
    project, _, _, _, receipt_id = seeded
    groups = AdmittedClusterReceiptReader(project).read(
        ClusterReceiptSource(kind="cluster_values", receipt_id=receipt_id)
    )
    keys = [group.key for group in reversed(groups)]
    request = _request(
        receipt_id,
        cluster_keys=keys,
        canonical_overrides={keys[0]: "Selected canonical"},
    )
    result = run_action_spec(project, request, project_id="p")
    assert result.status == "completed", result.errors
    sheet = result.outputs[0].sheet_id
    columns = {column["name"]: column["id"] for column in project.columns(sheet)}
    assert list(columns) == ["Canonical", "key", "variants", "mentions", "Sources"]
    row_ids = project.visible_row_ids(sheet)
    assert list(project.get_values(sheet, columns["key"]).values()) == keys
    assert (
        project.get_values(sheet, columns["Canonical"])[row_ids[0]]
        == "Selected canonical"
    )
    assert all(
        row[0] is None
        for row in project.db.execute(
            "SELECT parent_row_id FROM rows WHERE sheet_id=?", (sheet,)
        )
    )
    replay = run_action_spec(project, request, project_id="p")
    assert replay.receipt_id == result.receipt_id and replay.op_ids == result.op_ids
    project.apply_edits(
        [{"row_id": row_ids[0], "column_id": columns["Sources"], "value": []}]
    )
    stale = run_action_spec(project, request, project_id="p")
    assert stale.status == "failed" and stale.errors[0].code == "stale_replay"


def test_reviewed_overrides_and_exclusions_reach_entity_materialization(seeded):
    project, sheet, _, rows, receipt_id = seeded
    reader = AdmittedClusterReceiptReader(project)
    groups = reader.read(
        ClusterReceiptSource(kind="cluster_values", receipt_id=receipt_id)
    )
    jon = next(
        group for group in groups if any("Jon" in v.value for v in group.variants)
    )
    acme = next(group for group in groups if group.key != jon.key)
    reviewed = run_action_spec(
        project,
        {
            "action_id": "cluster.values",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "output_names": {"canonical": "Reviewed names"},
            "params": {
                "source": "name",
                "method": "fingerprint",
                "min_size": 2,
                "review": {
                    "source_hash": reader.facts[0]["source_value_hash"],
                    "canonical_overrides": {jon.key: "Jonathan Smith"},
                    "excluded_members": {acme.key: ["Acme Corp"]},
                },
            },
            "idempotency_key": "reviewed-clusters",
        },
        project_id="p",
    )
    assert reviewed.status == "completed", reviewed.errors
    result = run_action_spec(project, _request(reviewed.receipt_id), project_id="p")
    assert result.status == "completed", result.errors
    child = result.outputs[0].sheet_id
    columns = {col["name"]: col["id"] for col in project.columns(child)}
    assert list(project.get_values(child, columns["Canonical"]).values()) == [
        "Jonathan Smith"
    ]
    assert list(project.get_values(child, columns["mentions"]).values()) == [2]
    source_rows = {
        row[0]
        for row in project.db.execute(
            "SELECT source_row_id FROM materialized_row_sources WHERE op_id=?",
            (result.op_ids[0],),
        )
    }
    assert source_rows == set(rows[:2])


def test_source_change_after_reader_before_publication_writes_no_output(
    seeded, monkeypatch
):
    project, _, column, rows, receipt_id = seeded
    original = AdmittedClusterReceiptReader.read

    def read(self, source):
        result = original(self, source)
        self.project.apply_edits(
            [
                {
                    "row_id": rows[0],
                    "column_id": column,
                    "value": "changed after snapshot",
                }
            ]
        )
        return result

    monkeypatch.setattr(AdmittedClusterReceiptReader, "read", read)
    result = run_action_spec(project, _request(receipt_id), project_id="p")
    assert result.status == "failed" and result.errors[0].code == "stale_replay"
    assert (
        project.db.execute("SELECT 1 FROM sheets WHERE name='Entities'").fetchone()
        is None
    )
    assert (
        project.db.execute(
            "SELECT 1 FROM receipts WHERE idempotency_key='entities'"
        ).fetchone()
        is None
    )


def test_empty_cluster_result_retains_source_sheet_and_replays(seeded):
    project, sheet, _, _, _ = seeded
    result = run_action_spec(
        project,
        {
            "action_id": "cluster.values",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "output_names": {"canonical": "Small clusters"},
            "params": {
                "source": "name",
                "method": "fingerprint",
                "min_size": 20,
            },
            "idempotency_key": "empty-clusters",
        },
        project_id="p",
    )
    assert result.status == "completed", result.errors
    request = _request(result.receipt_id)
    materialized = run_action_spec(project, request, project_id="p")
    assert materialized.status == "completed", materialized.errors
    output_sheet = materialized.outputs[0].sheet_id
    assert project.visible_row_ids(output_sheet) == []
    assert (
        project.db.execute(
            "SELECT parent_sheet_id FROM sheets WHERE id=?", (output_sheet,)
        ).fetchone()[0]
        == sheet
    )
    assert (
        run_action_spec(project, request, project_id="p").receipt_id
        == materialized.receipt_id
    )


def test_empty_source_column_produces_consumable_empty_clusters(seeded):
    project, _, _, _, _ = seeded
    sheet = project.add_sheet("Empty source")
    project.add_column(sheet, "name")
    result = run_action_spec(
        project,
        {
            "action_id": "cluster.values",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": {"source": "name"},
            "idempotency_key": "empty-source-clusters",
        },
        project_id="p",
    )
    assert result.status == "completed", result.errors
    reader = AdmittedClusterReceiptReader(project)
    assert (
        reader.read(
            ClusterReceiptSource(kind="cluster_values", receipt_id=result.receipt_id)
        )
        == ()
    )
    assert reader.parent_sheet_id == sheet
    materialized = run_action_spec(project, _request(result.receipt_id), project_id="p")
    assert materialized.status == "completed", materialized.errors
    assert project.visible_row_ids(materialized.outputs[0].sheet_id) == []


def test_replay_refuses_removed_source_fence_evidence(seeded):
    project, _, _, _, receipt_id = seeded
    request = _request(receipt_id)
    result = run_action_spec(project, request, project_id="p")
    assert result.status == "completed", result.errors
    body = json.loads(
        project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()[0]
    )
    body["inputs"] = []
    project.db.execute(
        "UPDATE receipts SET body=? WHERE id=?", (json.dumps(body), result.receipt_id)
    )
    project.db.commit()
    replay = run_action_spec(project, request, project_id="p")
    assert replay.status == "failed" and replay.errors[0].code == "stale_replay"
