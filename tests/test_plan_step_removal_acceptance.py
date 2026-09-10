from __future__ import annotations

import dataclasses
import importlib.util
from pathlib import Path

from frisket.contracts.action import ActionIdentity, ActionResult, Receipt
from frisket.engine.executor.action_jobs import ActionJobEnvelope
from frisket.engine.executor.queued_actions import QueuedV1PayloadEnvelope
from frisket.engine.store import Project
from frisket.server.app import create_app
from helpers import run_cli


FALSE_PROVENANCE_FIELDS = {"plan_id", "step_id"}


def test_workflow_report_evaluator_and_cli_are_retired() -> None:
    assert importlib.util.find_spec("frisket.workflows.reports") is None
    assert importlib.util.find_spec("frisket.contracts.workflow_report") is None

    help_result = run_cli("--help")
    assert help_result.returncode == 0
    assert "frisket workflow" not in help_result.stdout
    assert "frisket action" in help_result.stdout


def test_public_action_contracts_and_new_projects_have_only_truthful_ids(
    tmp_path: Path,
) -> None:
    identity_fields = set(ActionIdentity.model_json_schema()["properties"])
    result_fields = set(ActionResult.model_json_schema()["properties"])
    receipt_fields = set(Receipt.model_json_schema()["properties"])

    assert not FALSE_PROVENANCE_FIELDS & identity_fields
    assert not FALSE_PROVENANCE_FIELDS & receipt_fields
    assert {"kind", "action_id"} <= identity_fields
    assert {"receipt_id", "action_id", "action_kind", "run_id"} <= receipt_fields
    assert {"receipt_id", "run_id", "job_id"} <= result_fields

    project = Project.create(tmp_path / "clean-receipts.frisket")
    try:
        receipt_columns = {
            str(row["name"])
            for row in project.db.execute("PRAGMA table_info(receipts)")
        }
    finally:
        project.close()

    assert not FALSE_PROVENANCE_FIELDS & receipt_columns
    assert {"id", "run_id", "action_kind", "action_id"} <= receipt_columns


def test_cli_and_http_action_ingress_do_not_accept_plan_or_step_labels(
    tmp_path: Path,
) -> None:
    cli_help = run_cli("action", "run", "--help")
    assert cli_help.returncode == 0
    assert "--plan-id" not in cli_help.stdout
    assert "--step-id" not in cli_help.stdout
    assert "--project" in cli_help.stdout

    document = create_app(tmp_path / "workspace").openapi()
    operation = document["paths"]["/api/projects/{pid}/actions/v1/run"]["post"]
    parameter_names = {item["name"] for item in operation.get("parameters", [])}
    assert not FALSE_PROVENANCE_FIELDS & parameter_names
    assert "pid" in parameter_names

    schemas = document["components"]["schemas"]
    receipt_summary_fields = set(schemas["ProvenanceReceiptSummary"]["properties"])
    assert not FALSE_PROVENANCE_FIELDS & receipt_summary_fields
    assert {"receipt_id", "action_kind", "run_id"} <= receipt_summary_fields


def test_action_job_queue_envelope_has_no_plan_or_step_labels() -> None:
    envelope_fields = {
        envelope.__name__: {field.name for field in dataclasses.fields(envelope)}
        for envelope in (ActionJobEnvelope, QueuedV1PayloadEnvelope)
    }
    for fields in envelope_fields.values():
        assert not FALSE_PROVENANCE_FIELDS & fields
    assert {
        "action_kind",
        "action_id",
        "receipt_id",
        "project_id",
    } <= envelope_fields["ActionJobEnvelope"]

    envelope = ActionJobEnvelope(
        action_kind="map.template",
        action_id="act_clean_cut",
        receipt_id="receipt_clean_cut",
        params_hash="sha256:clean-cut",
        idempotency_key="clean-cut@sha256:v1",
        project_id="project-clean-cut",
        action={"schema_version": "frisket.action.v2", "kind": "map.template"},
    )
    payload = envelope.to_json()
    assert not FALSE_PROVENANCE_FIELDS & payload.keys()
    assert payload["action_id"] == "act_clean_cut"
    assert payload["receipt_id"] == "receipt_clean_cut"
