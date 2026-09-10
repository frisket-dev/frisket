from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from executor_harness import CatalogEntry, ExecutorCase, Gate, case_env
from frisket.engine.store import Project
from action_test_helpers import write_json


def _plugin_manifest(
    *,
    plugin_id: str = "frisket-ndjson",
    version: str = "0.1.0",
    actions: list[str] | None = None,
    importers: list[str] | None = None,
    column_types: list[str] | None = None,
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "frisket.plugin.v1",
        "id": plugin_id,
        "version": version,
        "contributes": {
            "actions": actions if actions is not None else ["import.ndjson"],
            "importers": importers if importers is not None else ["ndjson"],
            "column_types": column_types if column_types is not None else ["geo_point"],
            "job_handlers": [],
        },
        "requires": {
            "capabilities": capabilities if capabilities is not None else [],
            "secrets": [],
        },
    }


def _write_manifest(
    tmp_path: Path,
    name: str = "frisket-ndjson.plugin.json",
    **overrides: Any,
) -> Path:
    package_dir = tmp_path / "plugin_packages" / Path(name).stem
    package_dir.mkdir(parents=True, exist_ok=True)
    return write_json(package_dir, "plugin.json", _plugin_manifest(**overrides))


def _plugin_load_action(
    manifest_path: Path,
    *,
    idempotency_key: str | None = "plugin_load@sha256:v1",
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "action_id": "plugin.load",
        "scope": {"kind": "project"},
        **({"capabilities": capabilities} if capabilities is not None else {}),
        "params": {
            "manifest": {"kind": "local_file", "path": str(manifest_path)},
        },
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del project
    from frisket.authoring.plugin_registry import _reset_default_registry_for_tests

    _reset_default_registry_for_tests()
    return {"dir": tmp_path, "manifest_path": _write_manifest(tmp_path)}


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _plugin_load_action(seeded["manifest_path"])


def _missing_key_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _plugin_load_action(seeded["manifest_path"], idempotency_key=None)


def _request_authored_capabilities_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _plugin_load_action(
        seeded["manifest_path"],
        idempotency_key="plugin_load@sha256:request-authored-capabilities",
        capabilities=["project:write"],
    )


def _unknown_contribution_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _plugin_load_action(
        _write_manifest(seeded["dir"], "invalid.plugin.json", actions=["import.magic"]),
        idempotency_key="plugin_load@sha256:invalid-manifest",
    )


def _unsupported_capability_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _plugin_load_action(
        _write_manifest(
            seeded["dir"],
            "unsupported-capabilities.plugin.json",
            capabilities=["custom:unimplemented"],
        ),
        idempotency_key="plugin_load@sha256:unsupported-capabilities",
    )


def _missing_source_action(seeded: dict[str, Any]) -> dict[str, Any]:
    manifest_path = _write_manifest(seeded["dir"], "missing-at-run.plugin.json")
    action = _plugin_load_action(
        manifest_path,
        idempotency_key="plugin_load@sha256:missing-source",
    )
    manifest_path.unlink()
    return action


def _bump_manifest_version(project: Project, seeded: dict[str, Any]) -> None:
    del project
    _write_manifest(seeded["dir"], "frisket-ndjson.plugin.json", version="0.2.0")


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt
    from frisket.authoring.plugin_registry import default_registry

    # Loading records the manifest in the receipt only; the process-wide
    # registry is never mutated by the executor.
    assert default_registry().plugin_manifests() == []

    assert result.outputs[0].kind == "plugin_manifest"
    output_ref = result.outputs[0].ref
    assert output_ref["kind"] == "plugin_manifest"
    assert output_ref["plugin_id"] == "frisket-ndjson"
    assert output_ref["version"] == "0.1.0"
    assert output_ref["contributes"]["actions"] == ["import.ndjson"]
    assert output_ref["manifest_sha256"].startswith("sha256:")

    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "plugin.load"
    assert receipt.inputs[0].name == "manifest"
    assert receipt.inputs[0].ref["path"] == str(seeded["manifest_path"])
    assert receipt.inputs[0].ref["request_hash"].startswith("sha256:")
    assert receipt.outputs[0].ref["plugin_id"] == "frisket-ndjson"
    assert (
        receipt.outputs[0].ref["request_hash"] == receipt.inputs[0].ref["request_hash"]
    )
    assert receipt.evidence[0].ref["kind"] == "plugin_manifest"


CASES = [
    ExecutorCase(
        kind="plugin.load",
        request_style="typed",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write", "plugin:load"),
            side_effects=frozenset(
                {
                    "read_plugin_manifest",
                    "validate_plugin_contributions",
                    "write_plugin_manifest_receipt",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_plugin_manifest_source",
                    "invalid_plugin_manifest",
                    "idempotency_conflict",
                }
            ),
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate(
                "missing_idempotency_key",
                _missing_key_action,
                "invalid_action_request",
            ),
            # This is strict envelope refusal, not host permission denial.
            # Activation trust and persisted permission gates remain covered by
            # the workbench backend activation/contribution registry suites.
            Gate(
                "request_authored_capabilities",
                _request_authored_capabilities_action,
                "invalid_action_request",
            ),
            Gate(
                "unknown_contribution",
                _unknown_contribution_action,
                "invalid_plugin_manifest",
            ),
            Gate(
                "unsupported_capability",
                _unsupported_capability_action,
                "invalid_plugin_manifest",
            ),
            Gate(
                "manifest_missing_at_run",
                _missing_source_action,
                "invalid_plugin_manifest_source",
            ),
            # Same key, same path, edited manifest content on disk: the stored
            # receipt's package evidence no longer matches.
            Gate(
                "idempotency_conflict",
                _make_action,
                "idempotency_conflict",
                after_primary_run=True,
                prepare=_bump_manifest_version,
            ),
        ),
        expect_counts={"receipts": 1},
        check_state=_check_state,
    )
]


def test_replay_survives_manifest_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay answers from the stored receipt, so the manifest file on disk is
    not required to still exist (or be readable) for an idempotent re-run."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        env.seeded["manifest_path"].unlink()
        before = env.counts()
        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert env.counts() == before


def test_toctou_manifest_rewrite_is_revalidated_at_run_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest that validated but was swapped for an invalid one before the
    run is re-read and rejected at execution time, writing nothing."""
    from frisket.actions.system import typed_action_for_request

    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        manifest_path = _write_manifest(env.seeded["dir"], "toctou.plugin.json")
        action = _plugin_load_action(
            manifest_path,
            idempotency_key="plugin_load@sha256:toctou",
        )
        prevalidated_action = typed_action_for_request(action)
        _write_manifest(
            env.seeded["dir"], "toctou.plugin.json", actions=["import.magic"]
        )

        def _prevalidated_plugin_load(_data):
            return prevalidated_action

        monkeypatch.setattr(
            "frisket.actions.system.typed_action_for_request", _prevalidated_plugin_load
        )
        before = env.counts()
        result = env.run(action)
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_plugin_manifest"
        assert env.counts() == before
