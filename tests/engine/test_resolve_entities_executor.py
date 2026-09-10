from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from executor_harness import CatalogEntry, ExecutorCase, Gate, case_env
from frisket.engine.store import Project
from helpers import replace_test_source_cell

PROJECT_ID = "project-resolve-entities"


def _cluster_action(sheet_id: int) -> dict[str, Any]:
    return {
        "action_id": "cluster.values",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": "name",
            "method": "fingerprint",
            "min_size": 2,
        },
        "idempotency_key": "cluster_values@sha256:resolve-v1",
    }


def _resolve_action(
    receipt_id: str,
    *,
    idempotency_key: str = "resolve_entities@sha256:v1",
    target_sheet_name: str = "Entities",
    cluster_keys: list[str] | None = None,
    canonical_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "source": {"kind": "cluster_values", "receipt_id": receipt_id},
    }
    if cluster_keys is not None:
        params["cluster_keys"] = cluster_keys
    if canonical_overrides is not None:
        params["canonical_overrides"] = canonical_overrides
    return {
        "action_id": "resolve.entities",
        "scope": {"kind": "project"},
        "sheet_name": target_sheet_name,
        "output_names": {
            key: key
            for key in ("entity", "key", "variants", "mentions", "source_variants")
        },
        "params": params,
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    """Seed the People sheet, then run cluster.values in-process so
    resolve.entities has a source receipt with a Jon Smith cluster."""
    from frisket.engine.executor import run_action_spec

    del tmp_path
    sheet_id = project.add_sheet("People")
    name_column_id = project.add_column(sheet_id, "name", type="text")
    row_ids = project.add_rows(
        sheet_id,
        [
            {"name": "Jon Smith"},
            {"name": "Smith, Jon"},
            {"name": "Jon Smith"},
            {"name": "Jane Doe"},
            {"name": "Acme Corp"},
            {"name": "Acme  Corp"},
        ],
        {"name": name_column_id},
    )
    cluster_result = run_action_spec(
        project,
        _cluster_action(sheet_id),
        project_id=PROJECT_ID,
    )
    assert cluster_result.status == "completed", cluster_result.errors
    from frisket.engine.store.receipts import ReceiptStore

    cluster_ref = next(
        item.ref
        for item in ReceiptStore(project)
        .parsed_by_id(cluster_result.receipt_id)
        .evidence
        if item.ref.get("kind") == "value_clusters"
    )
    return {
        "sheet_id": sheet_id,
        "name_column_id": name_column_id,
        "row_ids": row_ids,
        "cluster_receipt_id": cluster_result.receipt_id,
        "cluster_count": len(cluster_ref["clusters"]),
        "jon_key": cluster_ref["clusters"][0]["key"],
    }


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _resolve_action(
        seeded["cluster_receipt_id"],
        cluster_keys=[seeded["jon_key"]],
        canonical_overrides={seeded["jon_key"]: "Jonathan Smith"},
    )


def _legacy_capability_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _resolve_action(
        seeded["cluster_receipt_id"],
        idempotency_key="resolve_entities@sha256:legacy-capability",
    )
    action["capabilities"] = []
    return action


def _invalid_source_kind_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _resolve_action(
        seeded["cluster_receipt_id"],
        idempotency_key="resolve_entities@sha256:bad-source-kind",
    )
    action["params"]["source"]["kind"] = "loose_sheet_params"
    return action


def _missing_receipt_action(seeded: dict[str, Any]) -> dict[str, Any]:
    del seeded
    return _resolve_action(
        "receipt_missing",
        idempotency_key="resolve_entities@sha256:missing-receipt",
        target_sheet_name="Missing Receipt Entities",
    )


def _duplicate_sheet_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same target sheet name as the completed primary run, new key.
    return _resolve_action(
        seeded["cluster_receipt_id"],
        idempotency_key="resolve_entities@sha256:duplicate-sheet",
    )


def _conflict_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _resolve_action(
        seeded["cluster_receipt_id"],
        target_sheet_name="Other Entities",
        cluster_keys=[seeded["jon_key"]],
        canonical_overrides={seeded["jon_key"]: "Jonathan Smith"},
    )


def _column_id(project: Project, sheet_id: int, name: str) -> int:
    row = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name=? AND hidden=0",
        (sheet_id, name),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    assert result.run_id is None
    assert len(result.op_ids) == 1
    assert [output.kind for output in result.outputs] == [
        "sheet",
        *["column"] * 5,
        "rows",
    ]
    entity_sheet_id = result.outputs[0].sheet_id
    assert entity_sheet_id is not None
    entity_row_ids = next(
        output.row_ids for output in result.outputs if output.kind == "rows"
    )
    assert len(entity_row_ids) == 1
    entity_row_id = entity_row_ids[0]
    row_ids = seeded["row_ids"]
    jon_key = seeded["jon_key"]

    op = project.db.execute(
        "SELECT kind, status, spec FROM ops WHERE id=?", (result.op_ids[0],)
    ).fetchone()
    assert op is not None
    assert op["kind"] == "resolve.entities"
    assert op["status"] == "applied"
    op_spec = json.loads(op["spec"])
    assert op_spec["action_id"] == "resolve.entities"
    assert op_spec["scope"] == {"kind": "project"}
    assert op_spec["sheet_name"] == "Entities"
    assert op_spec["params_hash"].startswith("sha256:")
    assert op_spec["output_names"] == {
        name: name
        for name in ("entity", "key", "variants", "mentions", "source_variants")
    }
    params = op_spec["params"]
    assert params == {
        "source": {
            "kind": "cluster_values",
            "receipt_id": seeded["cluster_receipt_id"],
        },
        "cluster_keys": [jon_key],
        "canonical_overrides": {jon_key: "Jonathan Smith"},
    }
    (source,) = op_spec["reads"]
    assert source["kind"] == "cluster_receipt_source"
    assert source["receipt_id"] == seeded["cluster_receipt_id"]
    assert source["receipt_hash"].startswith("sha256:")
    assert source["sheet_id"] == seeded["sheet_id"]
    assert source["column_id"] == seeded["name_column_id"]
    assert source["column_name"] == "name"
    assert source["source_value_hash"].startswith("sha256:")
    source_receipt = Receipt.model_validate(
        json.loads(
            project.db.execute(
                "SELECT body FROM receipts WHERE id=?", (source["receipt_id"],)
            ).fetchone()["body"]
        )
    )
    source_refs = [
        item.ref
        for item in (
            *source_receipt.inputs,
            *source_receipt.outputs,
            *source_receipt.evidence,
        )
    ]
    cluster_preview = next(
        ref for ref in source_refs if ref["kind"] == "value_clusters"
    )
    source_values_ref = cluster_preview["source"]
    assert source_values_ref["row_ids"] == row_ids
    assert source["source_value_hash"] == source_values_ref["value_hash"]
    assert len(cluster_preview["clusters"]) == seeded["cluster_count"]

    sheets = {sheet["name"]: sheet["id"] for sheet in project.sheets()}
    assert sheets["Entities"] == entity_sheet_id
    entity_values = project.get_values(
        entity_sheet_id, _column_id(project, entity_sheet_id, "entity")
    )
    assert entity_values[entity_row_id] == "Jonathan Smith"
    key_values = project.get_values(
        entity_sheet_id, _column_id(project, entity_sheet_id, "key")
    )
    assert key_values[entity_row_id] == jon_key
    variants_values = project.get_values(
        entity_sheet_id, _column_id(project, entity_sheet_id, "variants")
    )
    assert variants_values[entity_row_id] == "Jon Smith, Smith, Jon"
    mentions_values = project.get_values(
        entity_sheet_id, _column_id(project, entity_sheet_id, "mentions")
    )
    assert mentions_values[entity_row_id] == 3
    source_variant_values = project.get_values(
        entity_sheet_id, _column_id(project, entity_sheet_id, "source_variants")
    )
    assert source_variant_values[entity_row_id] == [
        {"value": "Jon Smith", "count": 2, "row_ids": [row_ids[0], row_ids[2]]},
        {"value": "Smith, Jon", "count": 1, "row_ids": [row_ids[1]]},
    ]

    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "resolve.entities"
    assert receipt.status == "completed"
    assert receipt.run_id is None
    assert receipt.op_ids == result.op_ids
    # Local receipt reading is not a fabricated provider call.
    assert receipt.provider_use == []
    refs = [
        item.ref
        for section in (receipt.inputs, receipt.outputs, receipt.evidence)
        for item in section
    ]
    assert {
        "cluster_receipt_source",
        "materialized_sheet",
        "materialized_rows",
        "materialized_column",
        "materialized_row_sources",
    } <= {ref["kind"] for ref in refs}
    source_receipt_ref = next(
        ref for ref in refs if ref["kind"] == "cluster_receipt_source"
    )
    assert source_receipt_ref == source
    group = next(
        group for group in cluster_preview["clusters"] if group["key"] == jon_key
    )
    # Author overrides change the output, never the admitted source facts.
    assert group["canonical"] == "Jon Smith"
    assert group["size"] == 3
    assert group["row_ids"] == row_ids[:3]
    assert group["values"] == [
        {"value": variant["value"], "count": variant["count"]}
        for variant in source_variant_values[entity_row_id]
    ]
    row_ref = next(ref for ref in refs if ref["kind"] == "materialized_rows")
    assert row_ref["sheet_id"] == entity_sheet_id
    assert row_ref["row_ids"] == entity_row_ids
    assert row_ref["values_sha256"].startswith("sha256:")
    sheet_ref = next(ref for ref in refs if ref["kind"] == "materialized_sheet")
    assert sheet_ref["columns"] == {
        name: _column_id(project, entity_sheet_id, name)
        for name in ("entity", "key", "variants", "mentions", "source_variants")
    }


CASES = [
    ExecutorCase(
        kind="resolve.entities",
        request_style="typed",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            side_effects=frozenset(
                {
                    "create_sheet",
                    "create_columns",
                    "create_rows",
                    "write_op",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_input_ref",
                    "invalid_params",
                    "duplicate_sheet_name",
                    "stale_replay",
                    "idempotency_conflict",
                }
            ),
            cost_policy_kind="none",
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate(
                "legacy_capability_envelope",
                _legacy_capability_action,
                "invalid_action_request",
            ),
            Gate(
                "invalid_params_source_kind",
                _invalid_source_kind_action,
                "invalid_action_request",
            ),
            Gate(
                "invalid_input_ref_missing_receipt",
                _missing_receipt_action,
                "invalid_input_ref",
            ),
            Gate(
                "duplicate_sheet_name",
                _duplicate_sheet_action,
                "duplicate_sheet_name",
                after_primary_run=True,
            ),
            Gate(
                "idempotency_conflict",
                _conflict_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
        ),
        expect_counts={
            "sheets": 1,
            "columns": 5,
            "rows": 1,
            "ops": 1,
            "receipts": 1,
        },
        check_state=_check_state,
    )
]


def test_resolve_entities_normalizes_padded_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whitespace-padded target names, cluster keys, and overrides are
    normalized before hashing and materialization."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        jon_key = env.seeded["jon_key"]
        action = _resolve_action(
            env.seeded["cluster_receipt_id"],
            idempotency_key="resolve_entities@sha256:op-spec",
            target_sheet_name="  Resolved People  ",
            cluster_keys=[f" {jon_key} "],
            canonical_overrides={f" {jon_key} ": " Jonathan Smith "},
        )
        result = env.run(action)
        assert result.status == "completed", result.errors

        op_spec = json.loads(
            env.project.db.execute(
                "SELECT spec FROM ops WHERE id=?", (result.op_ids[0],)
            ).fetchone()["spec"]
        )
        params = op_spec["params"]
        assert op_spec["sheet_name"] == "Resolved People"
        assert params["cluster_keys"] == [jon_key]
        assert params["canonical_overrides"] == {jon_key: "Jonathan Smith"}
        sheets = {sheet["name"] for sheet in env.project.sheets()}
        assert "Resolved People" in sheets


def test_resolve_entities_stale_replay_rungs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay is refused as stale_replay when (a) the stored receipt's row
    refs are malformed, or (b) a materialized entity cell was tampered."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        entity_sheet_id = first.outputs[0].sheet_id
        entity_row_id = next(
            output.row_ids[0] for output in first.outputs if output.kind == "rows"
        )
        receipt_row = env.project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (first.receipt_id,)
        ).fetchone()
        original_body = json.loads(receipt_row["body"])

        malformed_body = json.loads(receipt_row["body"])
        for section in ("outputs", "evidence"):
            for item in malformed_body.get(section) or []:
                ref = item.get("ref") if isinstance(item, dict) else None
                if isinstance(ref, dict) and ref.get("kind") == "materialized_rows":
                    ref["row_ids"] = ["not-an-int"]
        env.project.db.execute(
            "UPDATE receipts SET body=? WHERE id=?",
            (json.dumps(malformed_body, sort_keys=True), first.receipt_id),
        )
        env.project.db.commit()
        malformed_replay = env.run_primary()
        assert malformed_replay.status == "failed"
        assert malformed_replay.errors[0].code == "stale_replay"

        env.project.db.execute(
            "UPDATE receipts SET body=? WHERE id=?",
            (json.dumps(original_body, sort_keys=True), first.receipt_id),
        )
        env.project.db.commit()
        replace_test_source_cell(
            env.project,
            row_id=entity_row_id,
            column_id=_column_id(env.project, entity_sheet_id, "entity"),
            value="Tampered Entity",
        )
        tampered_replay = env.run_primary()
        assert tampered_replay.status == "failed"
        assert tampered_replay.errors[0].code == "stale_replay"


def test_resolve_entities_stale_source_values_refuse_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the source column drifted since the cluster receipt was written, a
    fresh resolve refuses with stale_replay and writes nothing."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        replace_test_source_cell(
            env.project,
            row_id=env.seeded["row_ids"][0],
            column_id=env.seeded["name_column_id"],
            value="Jonathan Smith",
        )
        before = env.counts()
        stale = env.run(
            _resolve_action(
                env.seeded["cluster_receipt_id"],
                idempotency_key="resolve_entities@sha256:stale-source",
            )
        )
        assert stale.status == "failed"
        assert stale.errors[0].code == "stale_replay"
        assert env.counts() == before
