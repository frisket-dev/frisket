"""Paid previews share normal confirmation, execution and actual-call accounting."""

import asyncio
import json
import threading
from contextlib import closing
from dataclasses import replace
from decimal import Decimal

import pytest
from helpers import replace_test_source_cell

from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.contracts.action import Receipt
from frisket.engine.executor import resolve_map_preview
from frisket.engine.runner import MapRunner, validation
from frisket.engine.runner.preview import PreviewEffectRequiresRun
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import RouteStore, instance_principal
from frisket.engine.store.receipts import ReceiptStore
from frisket.execution.attempt import StaleAttemptWriter, claim
from frisket.execution.attempt_authority import attempt_identity
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.execution.resolve_for_action import record_resolved_execution
from tests.execution_composition_helpers import open_attempt_authority
from tests.preview.test_preview_inmemory import _summarize_action, _seed
from tests.engine.test_ocr_read import source as source
from tests.execution.test_resolve_for_action import audio_project as audio_project


class _Adapter:
    def __init__(self, *, cancel=None, invalid=False):
        self.calls = []
        self.cancel = cancel
        self.invalid = invalid

    async def complete(self, request, client):
        self.calls.append(request)
        if self.cancel is not None:
            self.cancel.set()
        value = {"other": "invalid"} if self.invalid else {"summary": "Paid sample."}
        return LLMResponse(
            content=json.dumps(value),
            data=value,
            tokens_in=20,
            tokens_out=5,
            cost=0.01,
            model=request.model,
            cost_source="model_metadata",
        )


def _runner(project, adapter):
    router = ModelRouter(keys={"anthropic": "test-key"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter
    coverage = ConsentCoverage(instance_principal(project), Decimal("0"))
    authority = replace(
        open_attempt_authority(project, router), consent_coverage=coverage
    )
    return MapRunner(
        project,
        router,
        authority=authority,
        consent_coverage=coverage,
        concurrency=1,
        execution_composition=authority.composition,
    )


def _admit(runner, plan):
    spec = dict(plan.runner_spec)
    with pytest.raises(validation.ClaimsGate) as raised:
        runner.prepare_preview(spec, program=plan.program, accounted=True)
    gate = raised.value
    assert gate.promise_set_hash
    spec = {
        **spec,
        "confirmed": True,
        "consented_promise_set_hash": gate.promise_set_hash,
    }
    prepared = runner.prepare_preview(spec, program=plan.program, accounted=True)
    project = runner.project
    receipt = Receipt(
        receipt_id="paid-preview",
        project_id="preview",
        action_id=spec["action_kind"],
        action_kind=spec["action_kind"],
        params_hash=attempt_identity(plan.program, spec),
        status="running",
    )
    ReceiptStore(project).insert_running(receipt)
    if prepared.resolved_execution is not None:
        record_resolved_execution(
            project,
            None,
            prepared.resolved_execution,
            receipt_id=receipt.receipt_id,
            spec=spec,
            consent_required=prepared.consent_required,
            consent_basis=prepared.consent_basis,
            consent_coverage=runner.consent_coverage,
        )
    else:
        assert prepared.unrouted_confirmation_hash
        RouteStore.for_receipt(project, receipt.receipt_id).record_consent(
            action_identity_hash=attempt_identity(plan.program, spec),
            promise_set_hash=prepared.unrouted_confirmation_hash,
            quote=prepared.unrouted_confirmation_quote,
            actor=instance_principal(project),
        )
    attempt = runner.authority.mint(
        receipt_id=receipt.receipt_id,
        recipe=plan.program,
        spec=spec,
        scope=tuple(prepared.row_ids),
    )
    claim(project, attempt, claimless_direct_effect=True)
    return spec, attempt


def _outputs(project):
    return {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "ops",
            "runs",
            "columns",
            "cells",
            "results",
            "output_column_claims",
        )
    }


@pytest.mark.parametrize("mode", ["success", "invalid", "cancel"])
def test_real_model_preview_accounts_success_failure_and_cancel(tmp_path, mode):
    with closing(Project.create(tmp_path / "preview.frisket")) as project:
        sheet, row_ids, _ = _seed(project, ["alpha", "beta"])
        cancelled = threading.Event()
        adapter = _Adapter(
            cancel=cancelled if mode == "cancel" else None, invalid=mode == "invalid"
        )
        runner = _runner(project, adapter)
        plan = resolve_map_preview(project, _summarize_action(sheet, row_ids=row_ids))
        before = _outputs(project)
        spec, attempt = _admit(runner, plan)
        result = asyncio.run(
            runner.preview(
                spec, program=plan.program, attempt=attempt, cancel_event=cancelled
            )
        )
        facts = runner.run_store.model_calls()
        assert adapter.calls and len(facts) == len(adapter.calls)
        assert sum(fact["provider_cost_usd"] for fact in facts) == pytest.approx(
            len(adapter.calls) * 0.01
        )
        assert {fact["attempt_id"] for fact in facts} == {attempt.attempt_id}
        assert all(
            fact["run_id"] is None and fact["column_id"] is None for fact in facts
        )
        if mode == "cancel":
            assert result.values == {} and len(adapter.calls) == 1
        elif mode == "invalid":
            assert all(
                cells["summary"].get("error") for cells in result.values.values()
            )
        else:
            assert all(
                cells["summary"]["value"] == "Paid sample."
                for cells in result.values.values()
            )
        assert _outputs(project) == before


@pytest.mark.parametrize("drift", ["scope", "source"])
def test_paid_preview_gate_does_not_accept_confirmation_or_attempt_for_other_work(
    tmp_path,
    drift,
):
    with closing(Project.create(tmp_path / "preview.frisket")) as project:
        sheet, rows, column = _seed(project, ["alpha", "beta"])
        adapter = _Adapter()
        runner = _runner(project, adapter)
        plan = resolve_map_preview(project, _summarize_action(sheet, row_ids=rows))
        spec = {
            **plan.runner_spec,
            "confirmed": True,
            "consented_promise_set_hash": "0" * 64,
        }
        with pytest.raises(validation.ClaimsGate):
            runner.prepare_preview(spec, program=plan.program, accounted=True)
        with pytest.raises(PreviewEffectRequiresRun):
            asyncio.run(runner.preview(spec, program=plan.program))
        exact, attempt = _admit(runner, plan)
        altered = {**exact, "row_ids": rows[:1]} if drift == "scope" else exact
        if drift == "source":
            replace_test_source_cell(
                project, row_id=rows[0], column_id=column, value="omega"
            )
        with pytest.raises((validation.CostGate, StaleAttemptWriter)):
            asyncio.run(runner.preview(altered, program=plan.program, attempt=attempt))
        assert adapter.calls == []


@pytest.mark.parametrize("terminal", ["success", "failure", "cancel", "fatal"])
def test_real_ocr_preview_retains_returned_capability_facts(
    source, monkeypatch, terminal
):
    from frisket.ai.models.metadata import ModelCallMeta
    from frisket.ops.ocr_engines import OcrEngines
    from frisket.engine.sandbox.shim import SandboxTeardownError

    project, sheet, _, row_id, _ = source
    cancelled = threading.Event()
    calls = []

    async def recognize(self, engine, pages, ctx, *, usage, **kwargs):
        calls.append(engine)
        usage["calls"] += 1
        usage["model_calls"] = [
            ModelCallMeta.provider_call(
                capability="ocr",
                engine=engine,
                provider="anthropic",
                provider_kind="chat_api",
                model_ids=["claude-haiku-4-5"],
                credential_source="local",
                provider_reported_cost_usd=0.03,
                provider_cost_usd=0.03,
                units={"input_pages": 1},
                cost_source="provider_reported",
                warnings=[],
                duration_ms=5,
            ).as_dict()
        ]
        if terminal == "cancel":
            cancelled.set()
            raise asyncio.CancelledError
        if terminal == "failure":
            raise ValueError("provider returned before decoding failed")
        if terminal == "fatal":
            raise SandboxTeardownError("owned process teardown failed")
        return [{"text": "Paid OCR sample", "blocks": []}]

    monkeypatch.setattr(OcrEngines, "run_engine_on_pages", recognize)
    runner = _runner(project, _Adapter())
    plan = resolve_map_preview(
        project,
        {
            "action_id": "media.ocr",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet, "row_ids": [row_id]},
            "params": {"source": "scan", "engine": "anthropic/claude-haiku-4-5"},
            "idempotency_key": "preview-ocr",
        },
    )
    before = _outputs(project)
    spec, attempt = _admit(runner, plan)
    if terminal in {"cancel", "fatal"}:
        error = asyncio.CancelledError if terminal == "cancel" else SandboxTeardownError
        with pytest.raises(error):
            asyncio.run(
                runner.preview(
                    spec, program=plan.program, attempt=attempt, cancel_event=cancelled
                )
            )
    else:
        result = asyncio.run(
            runner.preview(
                spec, program=plan.program, attempt=attempt, cancel_event=cancelled
            )
        )
        assert bool(result.values[row_id]["text"].get("error")) == (
            terminal == "failure"
        )
    [fact] = runner.run_store.model_calls()
    assert calls == ["anthropic/claude-haiku-4-5"]
    assert (
        fact["provider_cost_usd"] == 0.03 and fact["attempt_id"] == attempt.attempt_id
    )
    assert fact["epoch_id"] is not None
    assert _outputs(project) == before


@pytest.mark.parametrize("terminal", ["completed_then_cancel", "poll_failure"])
def test_paid_ocr_preview_acceptance_is_durable_before_poll(
    source, monkeypatch, terminal
):
    from frisket.execution.attempt import attempt_receipt, set_attempt_state
    from frisket.ops.base import RecipeInvocationHalt
    from frisket.ops.integrations.datalab import (
        DatalabEngineError,
        _accepted_submission_accounting,
        _complete_submission_accounting,
    )

    project, sheet, column, first, value = source
    [second] = project.add_rows(sheet, [{"scan": value}], {"scan": column})
    monkeypatch.setenv("DATALAB_API_KEY", "test-datalab-key")
    cancelled = threading.Event()
    accepted_ids = []

    async def ocr(
        http, key, data, filename, mime, *, on_accepted, credential_source, **kwargs
    ):
        accounting = _accepted_submission_accounting(
            {"request_check_url": "https://datalab.test/jobs/preview-one"},
            capability="ocr",
            credential_source=credential_source,
        )
        on_accepted(accounting)
        accepted_ids.append(accounting["model_calls"][0]["id"])
        [accepted] = project.db.execute("SELECT * FROM model_calls").fetchall()
        assert (
            accepted["id"] == accepted_ids[-1] and accepted["provider_cost_usd"] is None
        )
        assert accepted["run_id"] is None and accepted["attempt_id"] is not None
        if terminal == "poll_failure":
            raise DatalabEngineError(
                code="cancelled",
                message="Polling stopped after acceptance",
                accounting=accounting,
                provider_job_accepted=True,
            )
        _complete_submission_accounting(
            accounting, pages=1, cost_usd=0.01, duration_ms=7
        )
        return [{"text": "Paid first row", "blocks": []}], 0.01

    monkeypatch.setattr("frisket.ops.integrations.datalab.datalab_ocr", ocr)
    runner = _runner(project, _Adapter())
    plan = resolve_map_preview(
        project,
        {
            "action_id": "media.ocr",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet,
                "row_ids": [first, second],
            },
            "params": {"source": "scan", "engine": "datalab"},
            "idempotency_key": "priced-preview",
        },
    )
    before = _outputs(project)
    spec, attempt = _admit(runner, plan)
    execute = runner.preview(
        spec,
        program=plan.program,
        attempt=attempt,
        cancel_event=cancelled,
        progress_cb=lambda done, total: cancelled.set(),
    )
    if terminal == "poll_failure":
        with pytest.raises(RecipeInvocationHalt, match="accepted"):
            asyncio.run(execute)
    else:
        result = asyncio.run(execute)
        assert list(result.values) == [first]
    [fact] = runner.run_store.model_calls()
    assert fact["id"] == accepted_ids[0]
    assert fact["provider_cost_usd"] == (None if terminal == "poll_failure" else 0.01)
    assert fact["epoch_id"] is not None
    # The service terminalizes only after joined row work and capability drains.
    receipt = ReceiptStore(project).parsed_by_id(attempt.receipt_id)
    set_attempt_state(
        project,
        attempt.attempt_id,
        "halted" if terminal == "poll_failure" else "effected",
    )
    ReceiptStore(project).update_body_status(
        receipt.model_copy(
            update={
                "status": "failed" if terminal == "poll_failure" else "cancelled",
            }
        )
    )
    settled = attempt_receipt(project, attempt.attempt_id)["settlement"]
    if terminal == "poll_failure":
        assert settled["charge_usd"] is None
    else:
        assert settled["charge_usd"] is not None
    assert _outputs(project) == before


@pytest.mark.parametrize("cancel_during_provider", [False, True])
def test_priced_transcription_preview_settles_only_completed_quoted_rows(
    audio_project, monkeypatch, cancel_during_provider: bool
):
    from frisket.contracts.transcription_sidecar import TRANSCRIPTION_CONTRACT_VERSION
    from frisket.execution.attempt import attempt_receipt, set_attempt_state
    from frisket.execution.attempt_authority import AttemptAuthority
    from tests.execution.test_settlement_join import _execution_composition

    project, sheet = audio_project
    column = project.db.execute("SELECT id FROM columns WHERE name='media'").fetchone()[
        0
    ]
    [first] = project.visible_row_ids(sheet)
    value = project.get_values(sheet, column)[first]
    [second] = project.add_rows(sheet, [{"media": value}], {"media": column})
    cancelled = threading.Event()
    calls = []

    async def transcribe(ctx, endpoint, *, data, **kwargs):
        calls.append(data)
        if cancel_during_provider:
            cancelled.set()
        return {
            "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
            "results": [
                {
                    "engine": "parakeet-tdt",
                    "text": "Paid first row",
                    "segments": [],
                    "language": "en",
                    "duration": 2.0,
                    "model_ids": ["parakeet-tdt"],
                    "revision": "test",
                    "device": "cpu",
                    "dtype": "float32",
                    "timings": {"inference_seconds": 1.0},
                    "warnings": [],
                    "accepted_options": json.loads(data["options"]),
                }
            ],
        }

    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.sidecar.sidecar_post", transcribe
    )
    composition = _execution_composition()
    coverage = ConsentCoverage(instance_principal(project), Decimal("0"))
    runner = MapRunner(
        project,
        ModelRouter(),
        concurrency=1,
        authority=AttemptAuthority(
            project, composition=composition, consent_coverage=coverage
        ),
        consent_coverage=coverage,
        execution_composition=composition,
    )
    plan = resolve_map_preview(
        project,
        {
            "action_id": "media.transcribe",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet,
                "row_ids": [first, second],
            },
            "params": {"source": "media", "engine": "parakeet-tdt", "vad": False},
            "idempotency_key": "priced-transcribe-preview",
        },
    )
    before = _outputs(project)
    spec, attempt = _admit(runner, plan)
    result = asyncio.run(
        runner.preview(
            spec,
            program=plan.program,
            attempt=attempt,
            cancel_event=cancelled,
            progress_cb=lambda done, total: cancelled.set(),
        )
    )
    assert list(result.values) == ([] if cancel_during_provider else [first])
    assert len(calls) == 1
    assert [
        tuple(row)
        for row in project.db.execute(
            "SELECT row_id,terminal_outcome FROM attempt_row_authorizations ORDER BY row_id"
        )
    ] == [(first, "succeeded"), (second, "cancelled")]
    receipt = ReceiptStore(project).parsed_by_id(attempt.receipt_id)
    set_attempt_state(project, attempt.attempt_id, "effected")
    ReceiptStore(project).update_body_status(
        receipt.model_copy(update={"status": "cancelled"})
    )
    settled = attempt_receipt(project, attempt.attempt_id)["settlement"]
    assert settled["charge_usd"] is not None and float(settled["charge_usd"]) > 0
    assert settled["metered_quantity"] == "2"
    assert _outputs(project) == before
