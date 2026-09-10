from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from frisket.engine._workers import parakeet_artifacts as artifacts
from frisket.engine.sandbox.shim import SandboxResult, SandboxTeardownError


def _snapshot(
    cache: Path,
    *,
    repo_id: str,
    revision: str,
    files: tuple[str, ...],
    symlinks: bool = False,
) -> Path:
    repo = cache / f"models--{repo_id.replace('/', '--')}"
    snapshot = repo / "snapshots" / revision
    snapshot.mkdir(parents=True)
    for index, filename in enumerate(files):
        target = snapshot / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        if symlinks:
            blob = repo / "blobs" / f"blob-{index}"
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(b"fixture")
            target.symlink_to(blob)
        else:
            target.write_bytes(b"fixture")
    return snapshot


def _snapshots(cache: Path) -> tuple[Path, Path]:
    model = _snapshot(
        cache,
        repo_id=artifacts.PARAKEET_MODEL_REPO,
        revision=artifacts.PARAKEET_MODEL_REVISION,
        files=artifacts.PARAKEET_MODEL_FILES,
    )
    vad = _snapshot(
        cache,
        repo_id=artifacts.PARAKEET_VAD_REPO,
        revision=artifacts.PARAKEET_VAD_REVISION,
        files=artifacts.PARAKEET_VAD_FILES,
    )
    return model, vad


def test_resolver_uses_exact_public_pins_and_revalidates_parent_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "hub"
    model, vad = _snapshots(cache)
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))
    seen: dict[str, Any] = {}

    async def fake_run(argv, *, policy, stdin_data, should_cancel=None):
        seen.update(
            argv=argv,
            policy=policy,
            payload=json.loads(stdin_data),
            should_cancel=should_cancel,
        )
        return SandboxResult(
            returncode=0,
            stdout=json.dumps(
                {"ok": True, "model_path": str(model), "vad_path": str(vad)}
            ),
            stderr="",
        )

    monkeypatch.setattr(artifacts, "run_sandboxed", fake_run)

    def cancel() -> bool:
        return False

    resolved = asyncio.run(
        artifacts.resolve_parakeet_artifacts(vad=True, should_cancel=cancel)
    )

    assert resolved.model_path == model.resolve()
    assert resolved.vad_path == vad.resolve()
    assert resolved.cache_dir == cache.resolve()
    assert seen["should_cancel"] is cancel
    assert seen["policy"].allow_network is True
    assert seen["policy"].env_passthrough == ["VIRTUAL_ENV", "PYTHONPATH"]
    payload = seen["payload"]
    assert payload["model"] == {
        "repo_id": artifacts.PARAKEET_MODEL_REPO,
        "revision": artifacts.PARAKEET_MODEL_REVISION,
        "files": list(artifacts.PARAKEET_MODEL_FILES),
        "cache_dir": str(cache),
    }
    assert payload["vad"]["repo_id"] == artifacts.PARAKEET_VAD_REPO
    assert payload["vad"]["revision"] == artifacts.PARAKEET_VAD_REVISION
    assert payload["vad"]["files"] == list(artifacts.PARAKEET_VAD_FILES)
    assert "HF_TOKEN" not in seen["policy"].env_passthrough
    assert "HF_HOME" not in seen["policy"].env_passthrough


def test_child_snapshot_resolution_uses_hub_without_importing_onnx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "hub"
    model, _ = _snapshots(cache)
    calls: list[dict[str, Any]] = []
    fake_hub = ModuleType("huggingface_hub")

    def snapshot_download(repo_id, **kwargs):
        calls.append({"repo_id": repo_id, **kwargs})
        return str(model)

    fake_hub.snapshot_download = snapshot_download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)
    monkeypatch.delitem(sys.modules, "onnx_asr", raising=False)
    monkeypatch.delitem(sys.modules, "onnxruntime", raising=False)

    result = artifacts._resolve_snapshot(
        artifacts._snapshot_payload(
            repo_id=artifacts.PARAKEET_MODEL_REPO,
            revision=artifacts.PARAKEET_MODEL_REVISION,
            files=artifacts.PARAKEET_MODEL_FILES,
            cache_dir=cache,
        )
    )

    assert Path(result) == model.resolve()
    assert calls == [
        {
            "repo_id": artifacts.PARAKEET_MODEL_REPO,
            "revision": artifacts.PARAKEET_MODEL_REVISION,
            "cache_dir": cache,
            "allow_patterns": list(artifacts.PARAKEET_MODEL_FILES),
            "token": False,
            "max_workers": 4,
        }
    ]
    assert "onnx_asr" not in sys.modules
    assert "onnxruntime" not in sys.modules


@pytest.mark.parametrize("mode", ["cancelled", "failed", "spawn", "teardown"])
def test_resolver_failure_classes(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))

    async def fake_run(*args, **kwargs):
        if mode == "spawn":
            raise OSError("private path must not escape")
        if mode == "teardown":
            raise SandboxTeardownError("tree live")
        return SandboxResult(
            returncode=1 if mode == "failed" else 0,
            stdout="",
            stderr="private path must not escape",
            cancelled=mode == "cancelled",
        )

    monkeypatch.setattr(artifacts, "run_sandboxed", fake_run)
    expected = (
        SandboxTeardownError
        if mode == "teardown"
        else artifacts.ParakeetArtifactCancelled
        if mode == "cancelled"
        else artifacts.ParakeetArtifactUnavailable
    )
    with pytest.raises(expected) as caught:
        asyncio.run(artifacts.resolve_parakeet_artifacts(vad=False))
    assert "private path must not escape" not in str(caught.value)


def test_parent_rejects_wrong_repo_and_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "hub"
    valid, _ = _snapshots(cache)
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))

    wrong_repo = _snapshot(
        cache,
        repo_id="other/repository",
        revision=artifacts.PARAKEET_MODEL_REVISION,
        files=artifacts.PARAKEET_MODEL_FILES,
    )
    missing = valid
    (missing / artifacts.PARAKEET_MODEL_FILES[-1]).unlink()
    for path, revision in (
        (wrong_repo, artifacts.PARAKEET_MODEL_REVISION),
        (missing, artifacts.PARAKEET_MODEL_REVISION),
    ):
        with pytest.raises(artifacts.ParakeetArtifactUnavailable):
            artifacts._validate_snapshot(
                path,
                cache_dir=cache,
                repo_id=artifacts.PARAKEET_MODEL_REPO,
                revision=revision,
                files=artifacts.PARAKEET_MODEL_FILES,
            )


@pytest.mark.skipif(
    sys.platform == "win32", reason="ordinary Windows CI may not allow symlinks"
)
def test_parent_rejects_symlink_escape(tmp_path: Path) -> None:
    cache = tmp_path / "hub"
    escaped = _snapshot(
        cache,
        repo_id=artifacts.PARAKEET_MODEL_REPO,
        revision="f" * 40,
        files=artifacts.PARAKEET_MODEL_FILES,
        symlinks=True,
    )
    outside = tmp_path / "outside.onnx"
    outside.write_bytes(b"outside")
    escaped_file = escaped / artifacts.PARAKEET_MODEL_FILES[0]
    escaped_file.unlink()
    escaped_file.symlink_to(outside)

    with pytest.raises(artifacts.ParakeetArtifactUnavailable):
        artifacts._validate_snapshot(
            escaped,
            cache_dir=cache,
            repo_id=artifacts.PARAKEET_MODEL_REPO,
            revision="f" * 40,
            files=artifacts.PARAKEET_MODEL_FILES,
        )


def test_resolution_identity_comes_from_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repo/revision/file-list payload sent to the sandboxed child must
    come from the manifest lookup, not from a locally hard-coded value -- if
    it did not, editing the manifest entry would silently fail to affect
    what actually gets pulled."""
    from frisket.ai.models import artifact_manifest

    cache = tmp_path / "hub"
    model, _ = _snapshots(cache)
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))
    seen: dict[str, Any] = {}

    async def fake_run(argv, *, policy, stdin_data, should_cancel=None):
        seen["payload"] = json.loads(stdin_data)
        return SandboxResult(
            returncode=0,
            stdout=json.dumps({"ok": True, "model_path": str(model)}),
            stderr="",
        )

    monkeypatch.setattr(artifacts, "run_sandboxed", fake_run)

    resolved = asyncio.run(artifacts.resolve_parakeet_artifacts(vad=False))

    entry = artifact_manifest.parakeet_model_artifact()
    assert entry is not None and entry.hf_snapshot is not None
    assert seen["payload"]["model"]["repo_id"] == entry.hf_snapshot.repo_id
    assert seen["payload"]["model"]["revision"] == entry.hf_snapshot.revision
    assert seen["payload"]["model"]["files"] == list(entry.hf_snapshot.files)
    assert resolved.model_revision == entry.hf_snapshot.revision


def test_resolution_fails_loudly_if_the_manifest_entry_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.ai.models import artifact_manifest

    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    monkeypatch.setattr(artifact_manifest, "parakeet_model_artifact", lambda: None)

    with pytest.raises(artifacts.ParakeetArtifactUnavailable):
        asyncio.run(artifacts.resolve_parakeet_artifacts(vad=False))


def test_parent_rejects_malformed_or_extra_resolver_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "hub"
    model, _ = _snapshots(cache)
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))

    async def fake_run(*args, **kwargs):
        return SandboxResult(
            returncode=0,
            stdout=json.dumps(
                {"ok": True, "model_path": str(model), "unexpected": "field"}
            ),
            stderr="",
        )

    monkeypatch.setattr(artifacts, "run_sandboxed", fake_run)
    with pytest.raises(artifacts.ParakeetArtifactUnavailable):
        asyncio.run(artifacts.resolve_parakeet_artifacts(vad=False))
