"""Durable Parakeet engine setup dispatch without live downloads."""

from __future__ import annotations

import pytest

from frisket.engine.jobs import engine_setup, model_pull_store as store
from frisket.engine.jobs.queue import open_queue


@pytest.mark.parametrize("outcome", ["done", "cancelled", "failed"])
def test_chromium_setup_records_completion_cancel_or_retryable_failure(
    tmp_path, monkeypatch, outcome
):
    from frisket.runtime import chromium
    from frisket.runtime.model_install import ModelInstallCancelled

    def install(**kwargs):
        if outcome == "cancelled":
            raise ModelInstallCancelled
        if outcome == "failed":
            raise RuntimeError("download failed")

    monkeypatch.setattr(chromium, "install_chromium", install)
    queue = open_queue(workspace=tmp_path)
    try:
        row, _ = store.create_or_get_active(
            queue.engine,
            workspace_root=str(tmp_path),
            model_ref=engine_setup.CHROMIUM_SETUP_REF,
        )
        store.mark_running(queue.engine, row.id, job_id=1)
        args = dict(
            engine=queue.engine,
            pull_id=row.id,
            should_cancel=lambda: False,
            is_final_attempt=True,
        )
        if outcome == "failed":
            with pytest.raises(RuntimeError, match="browser_setup_failed"):
                engine_setup.run_engine_setup(**args)
        else:
            assert engine_setup.run_engine_setup(**args) == {"status": outcome}
        saved = store.get(queue.engine, row.id)
        assert saved.status == outcome
        assert store.to_dto(saved)["capabilities"]["retry"] is (outcome != "done")
    finally:
        queue.close()


def test_engine_setup_rejects_an_unhandled_setup_ref(tmp_path) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        row, _ = store.create_or_get_active(
            queue.engine,
            workspace_root=str(tmp_path),
            model_ref="engine-setup:future.local@1",
        )
        store.mark_running(queue.engine, row.id, job_id=1)

        with pytest.raises(LookupError, match="unsupported engine setup ref"):
            engine_setup.run_engine_setup(
                engine=queue.engine,
                pull_id=row.id,
                should_cancel=lambda: False,
                is_final_attempt=True,
            )
    finally:
        queue.close()


@pytest.mark.parametrize("outcome", ["ready", "cancelled", "incomplete", "offline"])
def test_multimodal_setup_publishes_only_a_complete_snapshot(
    tmp_path, monkeypatch, outcome
):
    from frisket.engine._workers import embeddinggemma_artifacts
    from frisket.engine.jobs import artifact_pull
    from frisket.runtime import embeddinggemma_install

    steps = []
    cancelled = False

    def install(**kwargs):
        steps.append("runtime")

    def download(engine, pull_id, pinned, **kwargs):
        nonlocal cancelled
        assert kwargs["mark_done"] is False
        assert store.get(engine, pull_id).status != store.STATUS_DONE
        steps.append(pinned.hf_snapshot.repo_id)
        if outcome == "offline":
            from frisket.engine.jobs.model_pull import _fail

            raise _fail(
                engine,
                pull_id,
                error_code="artifact_source_unreachable",
                message="Could not download the model.",
                terminal=False,
                is_final_attempt=True,
            )
        cancelled = outcome == "cancelled"
        return {"status": "done"}

    monkeypatch.setattr(embeddinggemma_install, "install_embeddinggemma", install)
    monkeypatch.setattr(artifact_pull, "_run_hf_snapshot_pull", download)
    monkeypatch.setattr(
        embeddinggemma_artifacts,
        "cached_embeddinggemma_path",
        lambda: None if outcome == "incomplete" else tmp_path,
    )
    queue = open_queue(workspace=tmp_path)
    try:
        row, _ = store.create_or_get_active(
            queue.engine,
            workspace_root=str(tmp_path),
            model_ref=engine_setup.EMBEDDINGGEMMA_SETUP_REF,
        )
        store.mark_running(queue.engine, row.id, job_id=1)
        assert store.to_dto(store.get(queue.engine, row.id))["display_name"] == (
            "EmbeddingGemma 2"
        )
        args = dict(
            engine=queue.engine,
            pull_id=row.id,
            should_cancel=lambda: cancelled,
            is_final_attempt=True,
        )
        if outcome in {"incomplete", "offline"}:
            code = (
                "artifact_source_unreachable"
                if outcome == "offline"
                else "engine_setup_unavailable"
            )
            with pytest.raises(RuntimeError, match=code):
                engine_setup.run_engine_setup(**args)
            assert store.get(queue.engine, row.id).status == store.STATUS_FAILED
            assert store.get(queue.engine, row.id).error_code == code
        else:
            result = engine_setup.run_engine_setup(**args)
            expected = "cancelled" if outcome == "cancelled" else "done"
            assert result == {"status": expected}
            assert store.get(queue.engine, row.id).status == expected
        assert steps == ["runtime", "google/embeddinggemma-2"]
    finally:
        queue.close()


@pytest.mark.parametrize("cancel_after_download", [False, True])
def test_indra_setup_installs_runtime_then_pinned_weights(
    tmp_path, monkeypatch, cancel_after_download
):
    from frisket.engine._workers import indra_ocr_artifacts
    from frisket.engine.jobs import artifact_pull
    from frisket.runtime import indra_ocr_install

    order = []
    cancelled = False

    def install(**kwargs):
        order.append("runtime")

    def download(engine, pull_id, pinned, **kwargs):
        nonlocal cancelled
        assert kwargs["mark_done"] is False
        order.append(pinned.hf_snapshot.repo_id)
        cancelled = cancel_after_download
        return {"status": "done"}

    monkeypatch.setattr(indra_ocr_install, "install_indra_ocr", install)
    monkeypatch.setattr(artifact_pull, "_run_hf_snapshot_pull", download)
    monkeypatch.setattr(indra_ocr_artifacts, "cached_indra_ocr_path", lambda: tmp_path)
    queue = open_queue(workspace=tmp_path)
    try:
        row, _ = store.create_or_get_active(
            queue.engine,
            workspace_root=str(tmp_path),
            model_ref=engine_setup.INDRA_OCR_SETUP_REF,
        )
        store.mark_running(queue.engine, row.id, job_id=1)
        result = engine_setup.run_engine_setup(
            engine=queue.engine,
            pull_id=row.id,
            should_cancel=lambda: cancelled,
            is_final_attempt=True,
        )
        expected = "cancelled" if cancel_after_download else "done"
        assert result == {"status": expected}
        assert store.get(queue.engine, row.id).status == expected
        assert order == ["runtime", "datalab-to/indra-ocr"]
    finally:
        queue.close()


@pytest.mark.parametrize("engine_id", ["gliclass", "jeff"])
@pytest.mark.parametrize("cancel_after_download", [False, True])
def test_classifier_setup_installs_runtime_then_selected_weights(
    tmp_path, monkeypatch, engine_id, cancel_after_download
):
    from frisket.contracts.classification import LOCAL_CLASSIFIERS
    from frisket.engine._workers import classifier_artifacts
    from frisket.engine.jobs import artifact_pull
    from frisket.runtime import classifier_install

    order = []
    cancelled = False

    def install(**kwargs):
        order.append("runtime")

    def download(engine, pull_id, pinned, **kwargs):
        nonlocal cancelled
        assert store.get(engine, pull_id).status != store.STATUS_DONE
        assert kwargs["mark_done"] is False
        order.append(pinned.hf_snapshot.repo_id)
        cancelled = cancel_after_download
        return {"status": "done"}

    monkeypatch.setattr(classifier_install, "install_classifiers", install)
    monkeypatch.setattr(artifact_pull, "_run_hf_snapshot_pull", download)
    monkeypatch.setattr(
        classifier_artifacts, "cached_classifier_path", lambda _: tmp_path
    )
    queue = open_queue(workspace=tmp_path)
    try:
        spec = LOCAL_CLASSIFIERS[engine_id]
        row, _ = store.create_or_get_active(
            queue.engine, workspace_root=str(tmp_path), model_ref=spec.setup_ref
        )
        store.mark_running(queue.engine, row.id, job_id=1)
        result = engine_setup.run_engine_setup(
            engine=queue.engine,
            pull_id=row.id,
            should_cancel=lambda: cancelled,
            is_final_attempt=True,
        )
        expected = "cancelled" if cancel_after_download else "done"
        assert result == {"status": expected}
        assert store.get(queue.engine, row.id).status == expected
        assert order == ["runtime", spec.model_repo]
    finally:
        queue.close()


def test_engine_setup_uses_existing_row_and_marks_done(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "frisket.execution.definitions.parakeet_runtime_present", lambda: True
    )
    queue = open_queue(workspace=tmp_path)
    try:
        row, created = store.create_or_get_active(
            queue.engine,
            workspace_root=str(tmp_path),
            model_ref=engine_setup.PARAKEET_TDT_SETUP_REF,
        )
        assert created
        store.mark_running(queue.engine, row.id, job_id=1)
        seen: dict[str, object] = {}

        async def fake_resolve(*, vad: bool, should_cancel, allow_download: bool):
            seen["vad"] = vad
            seen["cancelled"] = should_cancel()
            seen["allow_download"] = allow_download
            return object()

        monkeypatch.setattr(
            "frisket.engine._workers.parakeet_artifacts.resolve_parakeet_artifacts",
            fake_resolve,
        )
        result = engine_setup.run_engine_setup(
            engine=queue.engine,
            pull_id=row.id,
            should_cancel=lambda: False,
            is_final_attempt=True,
        )

        assert result == {"status": "done"}
        assert seen == {"vad": True, "cancelled": False, "allow_download": True}
        completed = store.get(queue.engine, row.id)
        assert completed is not None
        assert completed.status == store.STATUS_DONE
        assert completed.artifact_kind == "engine_setup"
    finally:
        queue.close()


def test_parakeet_setup_refuses_weights_when_runtime_is_missing(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "frisket.execution.definitions.parakeet_runtime_present", lambda: False
    )

    async def forbidden(**_kwargs):
        raise AssertionError("unusable Parakeet weights must not be downloaded")

    monkeypatch.setattr(
        "frisket.engine._workers.parakeet_artifacts.resolve_parakeet_artifacts",
        forbidden,
    )
    queue = open_queue(workspace=tmp_path)
    try:
        row, _ = store.create_or_get_active(
            queue.engine,
            workspace_root=str(tmp_path),
            model_ref=engine_setup.PARAKEET_TDT_SETUP_REF,
        )
        store.mark_running(queue.engine, row.id, job_id=1)

        with pytest.raises(RuntimeError, match="engine_setup_unavailable"):
            engine_setup.run_engine_setup(
                engine=queue.engine,
                pull_id=row.id,
                should_cancel=lambda: False,
                is_final_attempt=True,
            )
    finally:
        queue.close()


def test_rapidocr_setup_records_manifest_size_and_digest(tmp_path, monkeypatch) -> None:
    from frisket.ai.models.artifact_manifest import rapidocr_default_model_artifact
    from frisket.ops import ocr_engines_local

    seen = {}

    def provision(*, should_cancel, progress):
        assert should_cancel() is False
        progress(0, 16_631_306)
        progress(16_631_306, 16_631_306)
        seen["provisioned"] = True

    monkeypatch.setattr(
        ocr_engines_local, "provision_rapidocr_default_models", provision
    )
    queue = open_queue(workspace=tmp_path)
    try:
        row, _ = store.create_or_get_active(
            queue.engine,
            workspace_root=str(tmp_path),
            model_ref=engine_setup.RAPIDOCR_SETUP_REF,
        )
        store.mark_running(queue.engine, row.id, job_id=1)

        result = engine_setup.run_engine_setup(
            engine=queue.engine,
            pull_id=row.id,
            should_cancel=lambda: False,
            is_final_attempt=True,
        )

        artifact = rapidocr_default_model_artifact()
        completed = store.get(queue.engine, row.id)
        assert artifact is not None and completed is not None
        assert result == {"status": "done"}
        assert seen == {"provisioned": True}
        assert completed.total_bytes == artifact.total_size
        assert completed.completed_bytes == artifact.total_size
        assert completed.resolved_digest == artifact.composite_digest
        assert completed.artifact_license == artifact.license
    finally:
        queue.close()


def test_local_search_setup_requirements_are_manifest_sized(monkeypatch) -> None:
    from frisket.engine._workers import embeddinggemma_artifacts, parakeet_artifacts
    from frisket.execution import definitions
    from frisket.ops import ocr_engines_local
    from frisket.runtime import embeddinggemma_install

    monkeypatch.setattr(ocr_engines_local, "rapidocr_available", lambda: (True, None))
    monkeypatch.setattr(ocr_engines_local, "rapidocr_setup_ready", lambda: False)
    monkeypatch.setattr(definitions, "parakeet_runtime_present", lambda: False)
    monkeypatch.setattr(
        parakeet_artifacts,
        "parakeet_missing_download_bytes",
        lambda: 2_000_000,
    )
    monkeypatch.setattr(embeddinggemma_install, "is_installed", lambda: True)
    monkeypatch.setattr(
        embeddinggemma_artifacts, "cached_embeddinggemma_path", lambda: None
    )

    requirements = engine_setup.local_search_setup_requirements()

    assert [item.capability for item in requirements] == [
        "ocr",
        "transcription",
        "semantic_search",
    ]
    assert requirements[0].download_bytes_if_missing == 16_631_306
    assert (
        requirements[0]
        .artifact_refs[0]
        .endswith(
            "@sha256:5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5"
        )
    )
    assert requirements[0].download_size_basis == "exact_checksum"
    assert requirements[0].runtime_installable is False
    assert requirements[0].runtime_ready is True
    assert requirements[0].weights_ready is False
    assert requirements[1].download_bytes_if_missing == 2_000_000
    assert len(requirements[1].artifact_refs) == 2
    assert requirements[1].download_size_basis == "manifest_estimate"
    assert requirements[1].runtime_installable is False
    assert requirements[1].runtime_ready is False
    assert "standard" in (requirements[1].runtime_remediation or "")
    assert requirements[2].download_bytes_if_missing == 1_525_784_180
    assert requirements[2].runtime_ready is True
    assert len(requirements[2].artifact_refs) == 1
    assert requirements[2].download_size_basis == "manifest_estimate"
    assert requirements[2].runtime_installable is True


def test_local_search_setup_requirements_report_zero_cached_bytes(monkeypatch) -> None:
    from frisket.engine._workers import embeddinggemma_artifacts, parakeet_artifacts
    from frisket.ops import ocr_engines_local

    monkeypatch.setattr(ocr_engines_local, "rapidocr_available", lambda: (True, None))
    monkeypatch.setattr(ocr_engines_local, "rapidocr_setup_ready", lambda: True)
    monkeypatch.setattr(
        parakeet_artifacts, "parakeet_missing_download_bytes", lambda: 0
    )
    monkeypatch.setattr(
        embeddinggemma_artifacts, "cached_embeddinggemma_path", lambda: object()
    )

    requirements = engine_setup.local_search_setup_requirements()

    assert [item.download_bytes_if_missing for item in requirements] == [0, 0, 0]


def test_engine_setup_cancel_is_terminal_without_resolver(
    tmp_path, monkeypatch
) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        row, _ = store.create_or_get_active(
            queue.engine,
            workspace_root=str(tmp_path),
            model_ref=engine_setup.PARAKEET_TDT_SETUP_REF,
        )

        async def should_not_resolve(**_kwargs):
            raise AssertionError("cancelled setup must not start provisioning")

        monkeypatch.setattr(
            "frisket.engine._workers.parakeet_artifacts.resolve_parakeet_artifacts",
            should_not_resolve,
        )
        result = engine_setup.run_engine_setup(
            engine=queue.engine,
            pull_id=row.id,
            should_cancel=lambda: True,
            is_final_attempt=True,
        )

        assert result == {"status": "cancelled"}
        assert store.get(queue.engine, row.id).status == store.STATUS_CANCELLED
    finally:
        queue.close()


@pytest.mark.parametrize("readiness", ["ready", "failed", "cancelled"])
def test_docling_engine_setup_waits_for_server_before_marking_done(
    tmp_path, monkeypatch, readiness
) -> None:
    queue = open_queue(workspace=tmp_path)
    try:
        row, _ = store.create_or_get_active(
            queue.engine,
            workspace_root=str(tmp_path),
            model_ref=engine_setup.DOCLING_SETUP_REF,
        )
        seen = {}

        def fake_install_docling(*, should_cancel, progress):
            seen["cancelled"] = should_cancel()
            progress("installing")

        monkeypatch.setattr(
            "frisket.runtime.model_install.install_docling", fake_install_docling
        )
        monkeypatch.setenv("FRISKET_LOCAL_MODELS_URL", "http://127.0.0.1:1234")
        monkeypatch.setenv("FRISKET_LOCAL_MODELS_TOKEN", "private-test-token")
        cancelled = False

        def wait_until_ready(url, token, *, stopped):
            nonlocal cancelled
            assert url == "http://127.0.0.1:1234"
            assert token == "private-test-token"
            assert seen == {"cancelled": False}
            assert store.get(queue.engine, row.id).status != store.STATUS_DONE
            cancelled = readiness == "cancelled"
            return readiness == "ready"

        monkeypatch.setattr(
            "frisket.runtime.model_server.wait_until_ready", wait_until_ready
        )
        kwargs = dict(
            engine=queue.engine,
            pull_id=row.id,
            should_cancel=lambda: cancelled,
            is_final_attempt=True,
        )
        if readiness == "failed":
            with pytest.raises(RuntimeError, match="model_server_start_failed"):
                engine_setup.run_engine_setup(**kwargs)
            failed = store.get(queue.engine, row.id)
            assert failed.status == store.STATUS_FAILED
            assert "Docling is installed" in failed.error_message
        else:
            result = engine_setup.run_engine_setup(**kwargs)
            expected = "done" if readiness == "ready" else "cancelled"
            assert result == {"status": expected}
            assert store.get(queue.engine, row.id).status == expected

        assert seen == {"cancelled": False}
    finally:
        queue.close()
