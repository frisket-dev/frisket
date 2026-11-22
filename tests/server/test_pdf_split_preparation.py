from __future__ import annotations
from dataclasses import replace

from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
from frisket.ops.base import ActionProgramInvocationHalt

from frisket.server.services.pdf_split_preparation import (
    PdfSplitPageTarget,
    PdfSplitPreparationStore,
)
from frisket.engine.store.transactions import write_transaction


def target(occurrence="first", *, page=2, engine="tesseract"):
    return PdfSplitPageTarget(
        occurrence_key=occurrence,
        source_artifact_id=17,
        blob_hash="sha256:" + "a" * 64,
        source_column_id=8,
        page_number=page,
        ocr_action_params={"engine": engine},
    )


def test_same_bytes_share_page_work_but_keep_occurrence_bindings(tmp_path):
    with PdfSplitPreparationStore(tmp_path / "inventory.db") as store:
        store.admit(
            operation_id=4, generation="g1", targets=[target(), target("second")]
        )
        assert store.pending_count() == 1
        work = store.pending(limit=10)[0]
        assert work.target.page_number == 2
        assert store.occurrences(work.work_key) == ("first", "second")
        assert store.claim(work.work_key, generation="g1")
        assert not store.claim(work.work_key, generation="g1")
        store.finish(work.work_key, generation="g1", prepared_ref_id=29)
        assert store.pending_count() == 0
    with PdfSplitPreparationStore(tmp_path / "inventory.db") as reopened:
        assert reopened.cached_ref(target()) == 29


def test_finished_empty_ocr_is_cached_but_settings_and_pages_are_new_work(tmp_path):
    with PdfSplitPreparationStore(tmp_path / "inventory.db") as store:
        store.admit(operation_id=4, generation="g1", targets=[target()])
        work = store.pending(limit=1)[0]
        assert store.claim(work.work_key, generation="g1")
        store.finish(work.work_key, generation="g1", prepared_ref_id=29)
        store.admit(
            operation_id=4,
            generation="g2",
            targets=[target("again"), target(page=3), target(engine="rapidocr")],
        )
        assert store.pending_count() == 2
        assert store.cached_ref(target("again")) == 29


def test_cancelled_generation_cannot_complete_after_redo(tmp_path):
    with PdfSplitPreparationStore(tmp_path / "inventory.db") as store:
        store.admit(operation_id=4, generation="g1", targets=[target()])
        work = store.pending(limit=1)[0]
        assert store.claim(work.work_key, generation="g1")
        store.cancel_generation("g1")
        assert not store.finish(work.work_key, generation="g1", prepared_ref_id=29)
        assert store.pending_count() == 0
        store.admit(operation_id=4, generation="g2", targets=[target()])
        assert not store.finish(work.work_key, generation="g1", prepared_ref_id=29)
        assert store.pending_count() == 1


def _runtime(tmp_path, monkeypatch):
    import io
    import json
    from contextlib import contextmanager
    from types import SimpleNamespace

    from pypdf import PdfWriter
    from frisket.ai.llm import LLMResponse, ModelRouter
    from frisket.engine.store.citations import record_source_artifact
    from frisket.engine.store.change_log import append_change
    from frisket.server.services.action_preview_runs import ActionPreviewRunService
    from frisket.server.services.pdf_split_preparation import PdfSplitPreparationAdapter
    from frisket.server.workspace import Workspace

    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")

    class Provider:
        calls = 0
        text = "Read page"

        async def complete(self, request, client):
            self.calls += 1
            return LLMResponse(
                content=json.dumps({"text": self.text}),
                data={"text": self.text},
                tokens_in=20,
                tokens_out=5,
                cost=0.01,
                model=request.model,
                cost_source="model_metadata",
            )

    provider = Provider()
    router = ModelRouter(
        model_call_policy=LOCAL_PURCHASE_POLICY,
        keys={"anthropic": "test-key"},
        cache=None,
        cache_mode="off",
    )
    router._adapters["anthropic"] = provider
    workspace = Workspace(
        tmp_path / "workspace", router=router, enable_local_model_pull=False
    )
    workspace.create("Page OCR", project_id="pages")
    project = workspace.get("pages")
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=72, height=72)
    output = io.BytesIO()
    writer.write(output)
    digest = project.blob_store.put(output.getvalue())
    with write_transaction(project.db):
        artifact = record_source_artifact(
            project,
            artifact_kind="file",
            media_type="application/pdf",
            blob_hash=digest,
            filename="original.pdf",
            page_count=3,
        )
    operation_id = append_change(project, "media.ocr", label="Sample OCR")
    preview = ActionPreviewRunService(workspace)
    adapter = PdfSplitPreparationAdapter(
        workspace,
        preview_runs=preview,
        inventory_path=tmp_path / "inventory.db",
        project_id="pages",
    )
    read = PdfSplitPageTarget(
        "first",
        int(artifact["id"]),
        "sha256:" + digest,
        None,
        2,
        {"engine": "anthropic/claude-haiku-4-5", "keep_text_position": False},
    )

    @contextmanager
    def open_source():
        with project.blob_store.materialize(digest) as path:
            yield path

    admitted = SimpleNamespace(
        sha256=digest,
        page_start=1,
        page_end=3,
        source_column_id=None,
        filename="original.pdf",
        page_count=3,
        open_source=open_source,
    )
    return adapter, preview, project, provider, read, operation_id, admitted


def _wait_preview(preview, jobs):
    from tests.deterministic_time import controlled_time

    with controlled_time(timeout=10) as clock:
        clock.wait_until(
            lambda: all(
                preview._registry.get("pages", job["preview_id"]).finished_at
                is not None
                for job in jobs
            ),
            message="source-page OCR did not complete",
        )
    return [preview.get_preview("pages", job["preview_id"]).payload for job in jobs]


def test_actual_scratch_receipt_reads_original_page_once_and_retains_reference(
    tmp_path, monkeypatch
):
    import pytest
    from frisket.engine.store.prepared_content import PreparedContentStore

    adapter, preview, project, provider, read, op, _source = _runtime(
        tmp_path, monkeypatch
    )
    try:
        estimated = adapter.estimate(
            targets=[read, replace(read, occurrence_key="second")],
            plan_token="admitted-plan",
        )
        assert estimated["pages"] == 1
        assert estimated["estimate"]["requires_confirmation"]
        with pytest.raises(ValueError, match="exact claims"):
            adapter.approve(estimated, confirmation=None)
        assert provider.calls == 0
        jobs = adapter.start_preview(
            targets=[read, replace(read, occurrence_key="second")],
            plan_token="admitted-plan",
            operation_id=op,
            generation="preview-one",
            confirmation=estimated["estimate"]["promise_set_hash"],
        )
        result = _wait_preview(preview, jobs)
        assert result[0]["status"] == "done", result
        assert provider.calls == 1
        with PdfSplitPreparationStore(adapter.inventory_path) as store:
            ref = store.cached_ref(read)
            assert ref is not None
            assert store.occurrences(read.work_key) == ("first", "second")
        prepared = PreparedContentStore(project).resolve(ref)
        assert prepared.text == "Read page"
        assert prepared.covered_pages == (2,)
        assert prepared.pins[0].positions["source_page_number"] == 2
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
        assert (
            project.db.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0]
            == 1
        )
        assert project.db.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 1
        assert (
            adapter.estimate(targets=[read], plan_token="admitted-plan")["pages"] == 0
        )
        adapter.inventory_path = tmp_path / "reopened-inventory.db"
        assert adapter.cached_reference(read) == ref
        assert (
            adapter.estimate(targets=[read], plan_token="reopened-plan")["pages"] == 0
        )
    finally:
        preview._registry.shutdown()
        project.close()


def test_upload_sample_uses_existing_draft_cache_without_project_artifact(
    tmp_path, monkeypatch
):

    adapter, preview, project, provider, read, _op, source = _runtime(
        tmp_path, monkeypatch
    )
    try:
        project.db.execute("DELETE FROM source_artifacts")
        project.db.commit()
        read = replace(read, source_artifact_id=None)
        adapter.admitted_sources[read.occurrence_key] = source
        estimated = adapter.estimate(targets=[read], plan_token="upload-manifest")
        jobs = adapter.start_preview(
            targets=[read],
            plan_token="upload-manifest",
            operation_id=0,
            generation="upload-preview",
            confirmation=estimated["estimate"]["promise_set_hash"],
        )
        result = _wait_preview(preview, jobs)
        assert result[0]["status"] == "done", result
        assert provider.calls == 1
        with PdfSplitPreparationStore(adapter.inventory_path) as store:
            assert store.cached_preview(read)["text"] == "Read page"
            assert store.cached_ref(read) is None
        assert (
            project.db.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()[0]
            == 0
        )
        assert (
            adapter.estimate(targets=[read], plan_token="upload-manifest")["pages"] == 0
        )
    finally:
        preview._registry.shutdown()
        project.close()


def test_source_page_grant_refuses_expansion_settings_and_confirmation_drop(
    tmp_path, monkeypatch
):
    import pytest
    from frisket.engine.runner.validation import TrustedConsentRefused
    from frisket.server.services.pdf_import_ocr_consent import (
        PdfImportOcrGrant,
        source_page_ocr_authorizer,
    )
    from frisket.server.services.pdf_split_preparation import PdfSplitPageWork

    adapter, preview, project, provider, read, _op, _source = _runtime(
        tmp_path, monkeypatch
    )
    try:
        estimated = adapter.estimate(targets=[read], plan_token="admitted-plan")
        estimated["estimate"]["requires_confirmation"] = False
        with pytest.raises(ValueError, match="exact claims"):
            adapter.approve(estimated, confirmation=None)
        import copy

        tampered = copy.deepcopy(estimated)
        tampered["authorization"]["allocation"]["requires_confirmation"] = False
        with pytest.raises(ValueError, match="estimate changed"):
            adapter.approve(tampered, confirmation=None)
        adapter.approve(
            estimated, confirmation=estimated["estimate"]["promise_set_hash"]
        )
        with PdfSplitPreparationStore(adapter.inventory_path) as store:
            for changed in [
                replace(read, page_number=3),
                replace(read, source_column_id=99),
            ]:
                with pytest.raises(ValueError, match="outside the approved plan"):
                    store.admit(
                        operation_id=2,
                        generation="g2",
                        targets=[changed],
                        approved_plan_token="admitted-plan",
                    )
        grant = PdfImportOcrGrant.from_mapping(estimated["authorization"])
        authorizer = source_page_ocr_authorizer(
            grant,
            project_id="pages",
            plan_token="admitted-plan",
            target=read.consent_identity,
            work_key=read.work_key,
        )
        for changed in [
            replace(read, page_number=3),
            replace(
                read, ocr_action_params={**dict(read.ocr_action_params), "dpi": 300}
            ),
        ]:
            lease, prepared = adapter._prepare(
                PdfSplitPageWork(changed.work_key, 1, "g1", changed, None)
            )
            try:
                plan, _source, router, composition, context = prepared
                with pytest.raises(
                    TrustedConsentRefused, match="page, settings, route or estimate"
                ):
                    preview.start_scratch_preview(
                        "pages",
                        plan,
                        confirmation=None,
                        router=router,
                        composition=composition,
                        execution_context=context,
                        trusted_consent_authorizer=authorizer,
                    )
            finally:
                lease.close()
        assert provider.calls == 0
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    finally:
        preview._registry.shutdown()
        project.close()


def _admit_split(adapter, project, read, estimated):
    from frisket.engine.store.pdf_split import PdfSplitWriter, PdfSplitSource
    from frisket.server.services.pdf_split_preparation import PdfSplitChildBinding

    writer = PdfSplitWriter.start(
        project,
        session_id="split-one",
        writer_authority="generation-one",
        project_id="pages",
        idempotency_key="split-one",
        params_hash="split-one",
        action_id="split-one",
        receipt_id="split-receipt",
        columns=[{"name": "media", "type": "file"}, {"name": "text", "type": "text"}],
        sources=[PdfSplitSource("first", 1)],
    )
    value = {"blob": read.blob_hash.removeprefix("sha256:"), "filename": "child.pdf"}
    children = writer.publish_source([{"media": value, "text": ""}], expected_cursor=0)
    session = writer.session
    writer.finalize(expected_cursor=1)
    columns = {row["name"]: row["id"] for row in project.columns(writer.sheet_id)}
    binding = PdfSplitChildBinding(
        session.change_id,
        "generation-one",
        "split-one",
        "first",
        read.work_key,
        children[0],
        999,
        columns["media"],
        columns["text"],
        1,
        value,
        "",
    )
    adapter.approve(estimated, confirmation=estimated["estimate"]["promise_set_hash"])
    with PdfSplitPreparationStore(adapter.inventory_path) as store:
        store.admit(
            operation_id=session.change_id,
            generation="generation-one",
            session_id="split-one",
            targets=[read],
            bindings=[binding],
            approved_plan_token="admitted-plan",
        )
    return session


def test_automatic_page_fanout_is_owned_by_split_without_new_operation(
    tmp_path, monkeypatch
):
    from frisket.engine.store.prepared_content import PreparedContentStore

    adapter, preview, project, provider, read, _op, _source = _runtime(
        tmp_path, monkeypatch
    )
    try:
        published = []
        adapter.publish_child_pages = lambda bindings, ref: [
            published.append((binding, ref)) or True for binding in bindings
        ]
        estimated = adapter.estimate(targets=[read], plan_token="admitted-plan")
        writer = _admit_split(adapter, project, read, estimated)
        count = project.db.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
        jobs = adapter.start_pending(generation="generation-one")
        results = _wait_preview(preview, jobs)
        assert results[0]["status"] == "done", results
        assert len(published) == 1
        assert published[0][0].operation_id == writer.change_id
        assert (
            PreparedContentStore(project).resolve(published[0][1]).text == "Read page"
        )
        assert project.db.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == count
        assert provider.calls == 1
        project.undo()
        project.redo()
        with PdfSplitPreparationStore(adapter.inventory_path) as store:
            with store.db:
                store.db.execute("UPDATE pdf_split_page_bindings SET published=0")
            stale_work = store._work(
                store.db.execute(
                    "SELECT * FROM pdf_split_page_work WHERE generation='generation-one'"
                ).fetchone()
            )
        assert adapter.fanout(stale_work) == 0
        assert len(published) == 1
    finally:
        preview._registry.shutdown()
        project.close()


def test_undo_redo_while_provider_runs_never_revives_worker_or_refunds_call(
    tmp_path, monkeypatch
):
    import threading

    adapter, preview, project, provider, read, _op, _source = _runtime(
        tmp_path, monkeypatch
    )
    entered, release = threading.Event(), threading.Event()
    complete = provider.complete

    async def blocked(request, client):
        entered.set()
        assert release.wait(10)
        return await complete(request, client)

    provider.complete = blocked
    try:
        published = []
        adapter.publish_child_pages = lambda bindings, ref: [
            published.append((binding, ref)) or True for binding in bindings
        ]
        estimated = adapter.estimate(targets=[read], plan_token="admitted-plan")
        _admit_split(adapter, project, read, estimated)
        jobs = adapter.start_pending(generation="generation-one")
        assert entered.wait(5)
        project.undo()
        project.redo()
        release.set()
        results = _wait_preview(preview, jobs)
        assert results[0]["status"] == "done", results
        assert provider.calls == 1
        assert published == []
        with PdfSplitPreparationStore(adapter.inventory_path) as store:
            assert store.cached_ref(read) is None
            assert store.pending_count() == 0
        receipt = project.db.execute(
            "SELECT status FROM receipts WHERE id!='split-receipt'"
        ).fetchone()
        assert receipt[0] == "completed"
        assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1
    finally:
        release.set()
        preview._registry.shutdown()
        project.close()


def test_paid_page_finishing_after_stop_is_cached_without_publishing_or_rebilling(
    tmp_path, monkeypatch
):
    import threading
    from frisket.engine.store.prepared_content import PreparedContentStore

    adapter, preview, project, provider, read, op, _source = _runtime(
        tmp_path, monkeypatch
    )
    entered, release = threading.Event(), threading.Event()
    complete = provider.complete

    async def blocked(request, client):
        entered.set()
        assert release.wait(10)
        return await complete(request, client)

    provider.complete = blocked
    try:
        published = []
        adapter.publish_child_pages = lambda bindings, ref: [
            published.append((binding, ref)) or True for binding in bindings
        ]
        estimated = adapter.estimate(targets=[read], plan_token="admitted-plan")
        _admit_split(adapter, project, read, estimated)
        operations = project.db.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
        cells = [tuple(row) for row in project.db.execute("SELECT * FROM cells")]
        jobs = adapter.start_pending(generation="generation-one")
        assert entered.wait(5)
        adapter.stop(generation="generation-one")
        assert adapter.progress(generation="generation-one")["stopping"] == 1
        release.set()
        assert _wait_preview(preview, jobs)[0]["status"] == "cancelled"
        progress = adapter.progress(generation="generation-one")
        assert progress["cancelled"] == 1
        assert progress["completed"] == 0
        assert progress["pending_bindings"] == 0
        with PdfSplitPreparationStore(adapter.inventory_path) as store:
            row = store.db.execute(
                "SELECT status,prepared_ref_id FROM pdf_split_page_work"
            ).fetchone()
            assert row["status"] == "cancelled"
            ref_id = store.cached_ref(read)
            assert ref_id is not None
            assert row["prepared_ref_id"] == ref_id
        content = PreparedContentStore(project).resolve(ref_id)
        assert content.text == "Read page"
        assert content.covered_pages == (2,)
        assert content.pins[0].positions["ocr_action_params"] == dict(
            read.ocr_action_params
        )
        assert published == []
        assert [
            tuple(row) for row in project.db.execute("SELECT * FROM cells")
        ] == cells
        assert (
            project.db.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
            == operations
        )
        assert (
            project.db.execute(
                "SELECT status FROM receipts WHERE id!='split-receipt'"
            ).fetchone()[0]
            == "cancelled"
        )
        assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1
        assert adapter.start_pending(generation="generation-one") == ()
        # A later review can reuse both the inventory cache and the durable
        # original-page reference after that inventory has been discarded.
        assert adapter.estimate(targets=[read], plan_token="later-review")["pages"] == 0
        adapter.inventory_path = tmp_path / "later-inventory.db"
        assert adapter.cached_reference(read) == ref_id
        assert (
            adapter.start_preview(
                targets=[read],
                plan_token="later-review",
                operation_id=op,
                generation="later-generation",
                confirmation=None,
            )
            == ()
        )
        assert provider.calls == 1
    finally:
        release.set()
        preview._registry.shutdown()
        project.close()


def test_successfully_read_empty_upload_page_is_never_retried_as_missing(
    tmp_path, monkeypatch
):

    adapter, preview, project, provider, read, _op, source = _runtime(
        tmp_path, monkeypatch
    )
    provider.text = ""
    try:
        read = replace(read, source_artifact_id=None)
        adapter.admitted_sources[read.occurrence_key] = source
        estimated = adapter.estimate(targets=[read], plan_token="empty-page-plan")
        jobs = adapter.start_preview(
            targets=[read],
            plan_token="empty-page-plan",
            operation_id=0,
            generation="empty-preview",
            confirmation=estimated["estimate"]["promise_set_hash"],
        )
        results = _wait_preview(preview, jobs)
        assert results[0]["status"] == "done", results
        with PdfSplitPreparationStore(adapter.inventory_path) as store:
            assert store.cached_preview(read)["text"] == ""
        assert (
            adapter.estimate(targets=[read], plan_token="empty-page-plan")["pages"] == 0
        )
        assert provider.calls == 1
    finally:
        preview._registry.shutdown()
        project.close()


def test_running_original_page_refuses_second_generation_and_rescan_until_terminal(
    tmp_path, monkeypatch
):
    import threading
    import pytest
    from frisket.server.services.pdf_split_preparation import (
        PdfSplitPreparationInProgress,
    )

    adapter, preview, project, provider, read, op, _source = _runtime(
        tmp_path, monkeypatch
    )
    entered, release = threading.Event(), threading.Event()
    complete = provider.complete

    async def blocked(request, client):
        entered.set()
        assert release.wait(10)
        return await complete(request, client)

    provider.complete = blocked
    try:
        estimated = adapter.estimate(targets=[read], plan_token="same-page")
        jobs = adapter.start_preview(
            targets=[read],
            plan_token="same-page",
            operation_id=op,
            generation="first-flight",
            confirmation=estimated["estimate"]["promise_set_hash"],
        )
        assert entered.wait(5)
        assert preview._registry.has_active_jobs()
        # A second hosted worker has a separate in-memory registry, but must
        # respect the first worker's durable in-flight page claim.
        from frisket.server.services.action_preview_runs import ActionPreviewRunService
        from frisket.server.services.pdf_split_preparation import (
            PdfSplitPreparationAdapter,
        )

        other_preview = ActionPreviewRunService(adapter.workspace)
        try:
            other = PdfSplitPreparationAdapter(
                adapter.workspace,
                preview_runs=other_preview,
                inventory_path=adapter.inventory_path,
                project_id="pages",
            )
            assert other.progress(generation="first-flight")["running"] == 1
        finally:
            other_preview._registry.shutdown()
        with pytest.raises(
            PdfSplitPreparationInProgress, match="already being prepared"
        ):
            adapter.estimate(targets=[read], plan_token="second-page")
        with PdfSplitPreparationStore(adapter.inventory_path) as store:
            with pytest.raises(PdfSplitPreparationInProgress):
                store.admit(
                    operation_id=op,
                    generation="second-flight",
                    targets=[read],
                    approved_plan_token="same-page",
                )
        release.set()
        assert _wait_preview(preview, jobs)[0]["status"] == "done"
        assert provider.calls == 1
        assert adapter.estimate(targets=[read], plan_token="same-page")["pages"] == 0
        rescanned = adapter.estimate(
            targets=[read], plan_token="rescan", force_rescan=True
        )
        assert rescanned["pages"] == 1
        again = adapter.start_preview(
            targets=[read],
            plan_token="rescan",
            operation_id=op,
            generation="second-flight",
            confirmation=rescanned["estimate"]["promise_set_hash"],
            force_rescan=True,
        )
        assert _wait_preview(preview, again)[0]["status"] == "done"
        assert provider.calls == 2
    finally:
        release.set()
        preview._registry.shutdown()
        project.close()


def test_accepted_job_tracking_error_never_requeues_or_duplicates_paid_page(
    tmp_path, monkeypatch
):
    import threading

    adapter, preview, project, provider, read, op, _source = _runtime(
        tmp_path, monkeypatch
    )
    entered, release = threading.Event(), threading.Event()
    complete = provider.complete
    original_get = preview._registry.get

    async def blocked(request, client):
        entered.set()
        assert release.wait(10)
        return await complete(request, client)

    provider.complete = blocked
    try:
        estimated = adapter.estimate(targets=[read], plan_token="tracking-error")
        monkeypatch.setattr(
            preview._registry,
            "get",
            lambda *args: (_ for _ in ()).throw(RuntimeError("tracking unavailable")),
        )
        jobs = adapter.start_preview(
            targets=[read],
            plan_token="tracking-error",
            operation_id=op,
            generation="tracking-flight",
            confirmation=estimated["estimate"]["promise_set_hash"],
        )
        assert entered.wait(5)
        monkeypatch.setattr(preview._registry, "get", original_get)
        with PdfSplitPreparationStore(adapter.inventory_path) as store:
            row = store.db.execute(
                "SELECT status,job_id FROM pdf_split_page_work WHERE generation='tracking-flight'"
            ).fetchone()
            assert row["status"] == "running"
            assert row["job_id"] == jobs[0]["preview_id"]
        assert (
            adapter._start_pending(
                generation="tracking-flight", limit=4, explicit_preview=True
            )
            == ()
        )
        release.set()
        assert _wait_preview(preview, jobs)[0]["status"] == "done"
        assert provider.calls == 1
    finally:
        release.set()
        preview._registry.shutdown()
        project.close()


def test_admit_rejects_unapproved_growth_without_poisoning_existing_generation(
    tmp_path,
):
    import pytest

    with PdfSplitPreparationStore(tmp_path / "inventory.db") as store:
        store.admit(operation_id=4, generation="g1", targets=[target()])
        with pytest.raises(ValueError, match="Additional source pages"):
            store.admit(operation_id=4, generation="g1", targets=[target(page=3)])
        assert store.pending_count() == 1
        assert store.pending(limit=10)[0].target.page_number == 2


def test_independent_preview_inventories_share_one_original_page_flight(
    tmp_path, monkeypatch
):
    import threading
    import pytest
    from frisket.server.services.pdf_split_preparation import (
        PdfSplitPreparationAdapter,
        PdfSplitPreparationInProgress,
    )

    adapter, preview, project, provider, read, op, _source = _runtime(
        tmp_path, monkeypatch
    )
    other = PdfSplitPreparationAdapter(
        adapter.workspace,
        preview_runs=preview,
        inventory_path=tmp_path / "second-inventory.db",
        project_id="pages",
    )
    entered, release = threading.Event(), threading.Event()
    complete = provider.complete

    async def blocked(request, client):
        entered.set()
        assert release.wait(10)
        return await complete(request, client)

    provider.complete = blocked
    try:
        first_estimate = adapter.estimate(targets=[read], plan_token="first-tab")
        second_estimate = other.estimate(targets=[read], plan_token="second-tab")
        jobs = adapter.start_preview(
            targets=[read],
            plan_token="first-tab",
            operation_id=op,
            generation="first-tab",
            confirmation=first_estimate["estimate"]["promise_set_hash"],
        )
        assert entered.wait(5)
        with pytest.raises(
            PdfSplitPreparationInProgress, match="already being prepared"
        ):
            other.start_preview(
                targets=[read],
                plan_token="second-tab",
                operation_id=op,
                generation="second-tab",
                confirmation=second_estimate["estimate"]["promise_set_hash"],
            )
        other.stop(generation="second-tab")
        release.set()
        assert _wait_preview(preview, jobs)[0]["status"] == "done"
        assert provider.calls == 1
    finally:
        release.set()
        preview._registry.shutdown()
        project.close()


def test_background_source_page_carries_the_approved_nondefault_actor(
    tmp_path, monkeypatch
):
    from decimal import Decimal
    from frisket.execution.consent_coverage import ConsentCoverage

    adapter, preview, project, provider, read, _op, _source = _runtime(
        tmp_path, monkeypatch
    )
    original = preview.prepare_ocr_scratch_path

    def as_authenticated_user(*args, **kwargs):
        plan, source, router, composition, context = original(*args, **kwargs)
        return (
            replace(
                plan,
                consent_coverage=ConsentCoverage("user:approved-reader", Decimal("0")),
            ),
            source,
            router,
            composition,
            context,
        )

    try:
        monkeypatch.setattr(preview, "prepare_ocr_scratch_path", as_authenticated_user)
        estimated = adapter.estimate(targets=[read], plan_token="admitted-plan")
        assert estimated["authorization"]["actor"] == "user:approved-reader"
        monkeypatch.setattr(preview, "prepare_ocr_scratch_path", original)
        _admit_split(adapter, project, read, estimated)
        adapter.publish_child_pages = lambda bindings, ref: [
            True for binding in bindings
        ]
        jobs = adapter.start_pending(generation="generation-one")
        result = _wait_preview(preview, jobs)
        assert result[0]["status"] == "done", result
        actors = {
            str(row[0])
            for row in project.db.execute(
                "SELECT actor FROM consents WHERE subject_kind='receipt'"
            )
        }
        assert actors == {"user:approved-reader"}
        assert provider.calls == 1
    finally:
        preview._registry.shutdown()
        project.close()


def test_approved_page_estimate_ignores_only_confirmation_display(
    tmp_path, monkeypatch
):
    from frisket.engine.runner.validation import (
        TrustedConsentContext,
        TrustedConsentGrant,
        TrustedConsentRefusal,
    )
    from frisket.execution.resolve_for_action import SCRATCH_PREVIEW_UNIT_ID
    from frisket.server.services.action_preview_runs import _scratch_claims
    from frisket.server.services.pdf_import_ocr_consent import (
        PdfImportOcrGrant,
        source_page_ocr_authorizer,
    )
    from frisket.server.services.pdf_split_preparation import PdfSplitPageWork

    adapter, preview, project, provider, read, _op, _source = _runtime(
        tmp_path, monkeypatch
    )
    lease = None
    try:
        estimated = adapter.estimate(targets=[read], plan_token="preapproved-page")
        grant = PdfImportOcrGrant.from_mapping(estimated["authorization"])
        authorize = source_page_ocr_authorizer(
            grant,
            project_id="pages",
            plan_token="preapproved-page",
            target=read.consent_identity,
            work_key=read.work_key,
        )
        lease, prepared = adapter._prepare(
            PdfSplitPageWork(read.work_key, 0, "", read, None)
        )
        plan = prepared[0]
        resolved, estimate, _ = _scratch_claims(project, plan)
        context = TrustedConsentContext(
            spec=plan.spec,
            row_ids=(SCRATCH_PREVIEW_UNIT_ID,),
            resolved_execution=resolved,
            estimate=estimate,
            principal=grant.actor,
        )
        assert isinstance(authorize(context), TrustedConsentGrant)
        flipped = {
            **estimate,
            "requires_confirmation": not estimate["requires_confirmation"],
        }
        assert isinstance(
            authorize(replace(context, estimate=flipped)), TrustedConsentGrant
        )
        for field, value in (
            ("cost", estimate["cost"] + 1),
            ("billed_cost", estimate["billed_cost"] + 1),
            ("policy_id", "changed-policy"),
        ):
            changed = {**flipped, field: value}
            assert isinstance(
                authorize(replace(context, estimate=changed)), TrustedConsentRefusal
            ), field
        assert provider.calls == 0
    finally:
        if lease is not None:
            lease.close()
        preview._registry.shutdown()
        project.close()


def test_split_undo_stops_provider_retry_without_explicit_stop(tmp_path, monkeypatch):
    from frisket.ai.llm.types import LLMError

    adapter, preview, project, provider, read, _op, _source = _runtime(
        tmp_path, monkeypatch
    )

    async def refuse_after_undo(request, client):
        provider.calls += 1
        project.undo()
        project.redo()
        raise LLMError("rate limited", status=429, retryable=True)

    provider.complete = refuse_after_undo
    try:
        estimated = adapter.estimate(targets=[read], plan_token="admitted-plan")
        _admit_split(adapter, project, read, estimated)
        jobs = adapter.start_pending(generation="generation-one")
        results = _wait_preview(preview, jobs)
        assert provider.calls == 1
        assert results[0]["status"] == "cancelled", results
        with PdfSplitPreparationStore(adapter.inventory_path) as store:
            work = store.db.execute("SELECT status FROM pdf_split_page_work").fetchone()
            assert work[0] == "cancelled"
            assert store.cached_ref(read) is None
    finally:
        preview._registry.shutdown()
        project.close()


def _halt_first_attempt(adapter, code):
    prepare = adapter._prepare
    attempts = []

    def halting(work, **kwargs):
        lease, prepared = prepare(work, **kwargs)
        plan = prepared[0]
        run = plan.run

        async def halt_once(context):
            attempts.append(work.work_key)
            if len(attempts) == 1:
                raise ActionProgramInvocationHalt(code, "the local engine is in use")
            return await run(context)

        return lease, (replace(plan, run=halt_once), *prepared[1:])

    adapter._prepare = halting
    return attempts


def test_page_refused_by_a_busy_local_engine_runs_again_without_rebilling(
    tmp_path, monkeypatch
):
    adapter, preview, project, provider, read, _op, _source = _runtime(
        tmp_path, monkeypatch
    )
    try:
        adapter.publish_child_pages = lambda bindings, ref: [True for _ in bindings]
        estimated = adapter.estimate(targets=[read], plan_token="admitted-plan")
        _admit_split(adapter, project, read, estimated)
        attempts = _halt_first_attempt(adapter, "local_engine_busy")
        jobs = adapter.start_pending(generation="generation-one")
        assert _wait_preview(preview, jobs)[0]["status"] == "error"
        with PdfSplitPreparationStore(adapter.inventory_path) as store:
            work = store.db.execute(
                "SELECT status,error,job_id FROM pdf_split_page_work"
            ).fetchone()
        assert tuple(work) == ("pending", None, None)
        assert provider.calls == 0
        assert adapter.progress(generation="generation-one")["pending"] == 1

        again = adapter.start_pending(generation="generation-one")
        assert _wait_preview(preview, again)[0]["status"] == "done"
        assert attempts == [read.work_key, read.work_key]
        assert provider.calls == 1
        with PdfSplitPreparationStore(adapter.inventory_path) as store:
            assert (
                store.db.execute("SELECT status FROM pdf_split_page_work").fetchone()[0]
                == "completed"
            )
            assert store.cached_ref(read) is not None
        assert project.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1
    finally:
        preview._registry.shutdown()
        project.close()


def test_other_local_engine_halts_still_fail_the_page(tmp_path, monkeypatch):
    adapter, preview, project, provider, read, _op, _source = _runtime(
        tmp_path, monkeypatch
    )
    try:
        estimated = adapter.estimate(targets=[read], plan_token="admitted-plan")
        _admit_split(adapter, project, read, estimated)
        _halt_first_attempt(adapter, "local_session_failed")
        jobs = adapter.start_pending(generation="generation-one")
        assert _wait_preview(preview, jobs)[0]["status"] == "error"
        with PdfSplitPreparationStore(adapter.inventory_path) as store:
            status, error = store.db.execute(
                "SELECT status,error FROM pdf_split_page_work"
            ).fetchone()
        assert status == "failed" and "local engine is in use" in error
        assert adapter.start_pending(generation="generation-one") == ()
        assert provider.calls == 0
    finally:
        preview._registry.shutdown()
        project.close()
