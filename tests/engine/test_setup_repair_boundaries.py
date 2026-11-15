"""Explicit engine setup repairs private environments and respects artifact ownership."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
from threading import Event

from filelock import FileLock
import httpx
import pytest

from frisket.engine.jobs import artifact_pull, engine_setup, model_pull_store as store
from frisket.engine.jobs.artifact_ref import normalize_artifact_ref
from frisket.engine.jobs.queue import open_queue


@pytest.mark.parametrize(
    "family", ["spacy", "translation", "parakeet-tdt", "faster_whisper"]
)
def test_explicit_setup_health_checks_a_present_private_runtime(
    tmp_path, monkeypatch, family
):
    from frisket.ai.models import spacy_model
    from frisket.runtime import spacy_install, speech_install, translation_install

    language = family in {"spacy", "translation"}
    module = (
        spacy_install
        if family == "spacy"
        else translation_install
        if language
        else speech_install
    )
    monkeypatch.setattr(module, "runtime_present", lambda *args: True)
    monkeypatch.setattr(module, "host_present", lambda *args: False, raising=False)
    monkeypatch.setattr(artifact_pull, "provision_pinned", lambda *args, **kwargs: None)
    monkeypatch.setattr(spacy_model, "ensure_loadable_model_path", lambda: tmp_path)
    checks = []

    def unhealthy(**kwargs):
        checks.append("health")
        raise RuntimeError("the existing private runtime failed its import probe")

    monkeypatch.setattr(
        module,
        "install_spacy"
        if family == "spacy"
        else "install_translation"
        if language
        else "install_speech",
        unhealthy,
    )
    queue = open_queue(workspace=tmp_path)
    try:
        ref = (
            engine_setup.SPACY_SETUP_REF
            if family == "spacy"
            else engine_setup.OPUS_MT_SETUP_REF
            if language
            else engine_setup.FASTER_WHISPER_SETUP_REF
        )
        row, _ = store.create_or_get_active(
            queue.engine, workspace_root=str(tmp_path), model_ref=ref
        )
        kwargs = dict(engine=queue.engine, pull_id=row.id, should_cancel=lambda: False)
        if language:
            with pytest.raises(RuntimeError, match="engine_setup_unavailable"):
                engine_setup.run_engine_setup(**kwargs, is_final_attempt=True)
            assert store.get(queue.engine, row.id).status == store.STATUS_FAILED
        else:
            with pytest.raises(RuntimeError, match="failed its import probe"):
                engine_setup._install_speech_runtime(family, **kwargs)
        assert checks == ["health"]
    finally:
        queue.close()


def test_spacy_repair_waits_for_artifact_owner_and_keeps_its_verified_replacement(
    tmp_path, monkeypatch
):
    from frisket.ai.models import artifact_manifest, model_cache, spacy_model
    from frisket.runtime import spacy_install

    cache = tmp_path / "cache"
    monkeypatch.setenv("FRISKET_MODEL_CACHE_DIR", str(cache))
    monkeypatch.setattr(spacy_install, "runtime_present", lambda: True)
    monkeypatch.setattr(spacy_install, "host_present", lambda: True, raising=False)
    payload = b"fixed"
    original = artifact_manifest.spacy_model_artifact()
    pinned = replace(
        original,
        files=(
            replace(
                original.files[0],
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
        ),
    )
    monkeypatch.setattr(artifact_manifest, "spacy_model_artifact", lambda: pinned)
    wheel = spacy_model.wheel_path()
    wheel.parent.mkdir(parents=True)
    wheel.write_bytes(b"wrong")
    monkeypatch.setattr(spacy_model, "ensure_loadable_model_path", lambda: tmp_path)
    acquiring = Event()
    acquire = artifact_pull._acquire_artifact_lock

    def observe_wait(*args, **kwargs):
        acquiring.set()
        return acquire(*args, **kwargs)

    monkeypatch.setattr(artifact_pull, "_acquire_artifact_lock", observe_wait)
    client_class = httpx.Client
    requests = []

    def unexpected_download(request):
        requests.append(request)
        raise AssertionError("the owner already repaired this exact artifact")

    queue = open_queue(workspace=tmp_path / "workspace")
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda: client_class(transport=httpx.MockTransport(unexpected_download)),
    )
    try:
        row, _ = store.create_or_get_active(
            queue.engine,
            workspace_root=str(tmp_path),
            model_ref=engine_setup.SPACY_SETUP_REF,
        )
        art = normalize_artifact_ref(pinned.ref)
        lock_path = model_cache.artifact_lock_path(art, root=cache)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with FileLock(lock_path):
                future = pool.submit(
                    engine_setup.run_engine_setup,
                    engine=queue.engine,
                    pull_id=row.id,
                    should_cancel=lambda: False,
                    is_final_attempt=True,
                )
                assert acquiring.wait(5)
                assert wheel.read_bytes() == b"wrong", (
                    "waiting setup mutated another artifact owner's cache"
                )
                wheel.write_bytes(payload)
            assert future.result(timeout=5) == {"status": "done"}
        assert wheel.read_bytes() == payload
        assert requests == []
    finally:
        queue.close()


@pytest.mark.parametrize("outcome", ["repaired", "checksum_failure", "cancelled"])
def test_verified_repair_promotes_only_complete_bytes_and_replaces_derived_cache(
    tmp_path, outcome
):
    from frisket.ai.models import artifact_manifest, model_cache

    payload = b"fixed"
    original = artifact_manifest.spacy_model_artifact()
    pinned = replace(
        original,
        files=(
            replace(
                original.files[0],
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
        ),
    )
    art = normalize_artifact_ref(pinned.ref)
    installed = model_cache.artifact_install_dir(art, root=tmp_path)
    installed.mkdir(parents=True)
    wheel = installed / pinned.files[0].repo_relpath
    wheel.write_bytes(b"wrong")
    derived = installed / "runtime" / "old-pipeline"
    derived.parent.mkdir()
    derived.write_bytes(b"old derived model")
    cancelled = False

    def download(request):
        nonlocal cancelled
        cancelled = outcome == "cancelled"
        return httpx.Response(
            200, content=b"wrong" if outcome == "checksum_failure" else payload
        )

    with httpx.Client(transport=httpx.MockTransport(download)) as client:

        def repair():
            artifact_pull.provision_pinned(
                art,
                pinned,
                cache_root=tmp_path,
                client=client,
                should_cancel=lambda: cancelled,
                verify_existing=True,
            )

        if outcome == "repaired":
            repair()
            assert wheel.read_bytes() == payload
            assert not derived.exists()
        else:
            expected = (
                artifact_pull.ProvisionCancelled
                if outcome == "cancelled"
                else artifact_pull.ArtifactProvisionError
            )
            with pytest.raises(expected):
                repair()
            assert wheel.read_bytes() == b"wrong"
            assert derived.read_bytes() == b"old derived model"
    assert not any((tmp_path / ".pull-tmp").iterdir())
