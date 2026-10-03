from frisket.engine._workers import classifier_artifacts as artifacts


def test_readiness_requires_runtime_and_complete_pinned_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    monkeypatch.setattr(artifacts, "is_installed", lambda: True)
    artifact = artifacts.classifier_artifact("gliclass")
    source = artifact.hf_snapshot
    root = tmp_path / f"models--{source.repo_id.replace('/', '--')}"
    snapshot = root / "snapshots" / source.revision
    snapshot.mkdir(parents=True)
    assert not artifacts.classifier_ready("gliclass")
    for filename in source.files:
        path = snapshot / filename
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text("fixture")
    assert artifacts.classifier_ready("gliclass")
    assert artifacts.cached_classifier_path("gliclass") == snapshot
    monkeypatch.setattr(artifacts, "is_installed", lambda: False)
    assert not artifacts.classifier_ready("gliclass")


def test_snapshot_symlink_outside_cache_is_not_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "cache"))
    source = artifacts.classifier_artifact("jeff").hf_snapshot
    snapshot = (
        tmp_path
        / "cache"
        / f"models--{source.repo_id.replace('/', '--')}"
        / "snapshots"
        / source.revision
    )
    snapshot.mkdir(parents=True)
    for filename in source.files:
        (snapshot / filename).write_text("fixture")
    escaped = tmp_path / "elsewhere"
    escaped.write_text("not-a-model")
    victim = snapshot / source.files[0]
    victim.unlink()
    try:
        victim.symlink_to(escaped)
    except OSError:
        import pytest

        pytest.skip("symlink creation requires OS privileges")
    assert artifacts.cached_classifier_path("jeff") is None
