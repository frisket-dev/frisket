"""Behavioral coverage for the ``frisket doctor`` self-probe."""

import re
import subprocess
import sys
from types import ModuleType
from pathlib import Path

import pytest

from frisket.operability import diagnostics
from frisket.ai.models.spacy_model import SpacyModelState

# The venv console script for [project.scripts] frisket, invoked directly
# rather than through `uv run`: uv's env-management chatter (venv rebuilds,
# sync warnings) lands on stderr in hermetic environments and would fail
# assertions that only care about frisket's own output.
FRISKET_CLI = str(Path(sys.executable).with_name("frisket"))


def test_doctor_exits_zero_and_probes_core():
    proc = subprocess.run(
        [FRISKET_CLI, "doctor"], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert "project store + search" in out and "✓" in out
    assert "HEALTHY" in out


def test_doctor_help_lists_script_backed_subcommands_without_running_probe():
    proc = subprocess.run(
        [FRISKET_CLI, "doctor", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "frisket doctor embeddings [--json]" in proc.stdout
    assert "project store + search" not in proc.stdout
    assert proc.stderr == ""


def test_doctor_function_reports_failures(monkeypatch):
    # a core failure must flip the exit code (doctor can actually fail)
    import frisket.cli as cli

    def boom():
        raise RuntimeError("simulated store failure")

    monkeypatch.setattr(cli, "doctor_cmd", cli.doctor_cmd)  # keep real fn referenced
    # simulate by breaking the store import path the probe uses
    import frisket.engine.store

    monkeypatch.setattr(
        frisket.engine.store.Project, "create", staticmethod(lambda *a, **k: boom())
    )
    assert cli.doctor_cmd() == 1


# ---------------------------------------------------------------------------
# local-engine readiness and managed-model states
#
# An importable engine package only means the code is there; whether its
# WEIGHTS are already on disk (offline-ready) or still need a Hugging Face
# fetch on first use is a separate, per-engine fact someone about to run a
# sensitive source through local ASR deserves to see before it happens.


_ENGINE_LABEL = (
    r"\w+ \((provisioned"
    r"|provisioned: default det/cls/rec"
    r"|provisioned: base pinned & pullable; other sizes require pre-populated HF cache"
    r"|fetches on first use"
    r"|not provisioned; offline worker"
    r"|not provisioned; pre-populated HF cache required"
    r"|model not yet downloaded"
    r"|model hash mismatch)\)"
)


@pytest.mark.parametrize(
    ("status", "source", "summary"),
    [
        ("present", "managed_cache", "present (managed_cache)"),
        ("not_downloaded", None, "not yet downloaded"),
        ("hash_mismatch", "managed_cache", "hash mismatch"),
    ],
)
def test_spacy_model_doctor_reports_bounded_state(monkeypatch, status, source, summary):
    from frisket.ai.models import spacy_model

    monkeypatch.setattr(
        spacy_model,
        "model_state",
        lambda: SpacyModelState(
            status=status,
            source=source,
            ref="spacy:en_core_web_sm@3.8.0",
            detail="bounded detail",
        ),
    )

    report = diagnostics.spacy_model_report()
    assert report["status"] == status
    assert summary in report["summary"]
    assert report["ref"] == "spacy:en_core_web_sm@3.8.0"

    full = diagnostics.run_diagnostics()
    assert full["info"]["spacy_model"]["status"] == status


@pytest.mark.parametrize(
    ("status", "label", "is_provisioned"),
    [
        ("present", "spacy (provisioned)", True),
        ("not_downloaded", "spacy (model not yet downloaded)", False),
        ("hash_mismatch", "spacy (model hash mismatch)", False),
    ],
)
def test_engines_report_labels_managed_spacy_state(
    monkeypatch, status, label, is_provisioned
):
    """The engine summary preserves the managed model's explicit state."""

    monkeypatch.setitem(sys.modules, "spacy", ModuleType("spacy"))
    monkeypatch.setattr(
        diagnostics,
        "_engine_provisioned",
        lambda name: is_provisioned if name == "spacy" else True,
    )
    monkeypatch.setattr(
        diagnostics,
        "spacy_model_report",
        lambda: {
            "status": status,
            "summary": status,
            "ref": "spacy:en_core_web_sm@3.8.0",
        },
    )

    report = diagnostics.engines_report()

    assert label in report["summary"]
    bucket = "provisioned" if is_provisioned else "fetches_on_first_use"
    assert "spacy" in report[bucket]
    if is_provisioned:
        assert report["remediation"] is None
    else:
        assert "spacy:en_core_web_sm@3.8.0" in report["remediation"]


def test_doctor_local_engines_line_labels_every_engine_state():
    """Whatever engines are actually importable in this environment, the
    real CLI line must tag each one — never a bare, ambiguous name — and any
    engine that is not ready comes with pre-provisioning remediation."""
    proc = subprocess.run(
        [FRISKET_CLI, "doctor"], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if "local engines:" in ln)
    body = line.split("local engines:", 1)[1].strip()
    assert body == (
        "none installed (repair base install; ASR needs frisket-data[standard])"
    ) or re.fullmatch(
        rf"({_ENGINE_LABEL})(, {_ENGINE_LABEL})*",
        body,
    ), body
    if "fetches on first use)" in body or "not provisioned; offline worker)" in body:
        assert "to provision ahead of a sensitive run" in proc.stdout, proc.stdout


def test_fastembed_cache_present_reads_dockerfile_warm_up_location(
    monkeypatch, tmp_path
):
    """The Dockerfile warms fastembed's embedding + rerank models into
    FASTEMBED_CACHE_PATH (or fastembed's own $TMPDIR/fastembed_cache
    default) at build time; the probe must read that exact location."""
    cache = tmp_path / "fastembed_cache"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(cache))
    assert diagnostics._fastembed_cache_present() is False  # not warmed yet

    cache.mkdir()
    (cache / "some-model").write_text("weights")
    assert diagnostics._fastembed_cache_present() is True


@pytest.fixture
def rapidocr_model_resolver():
    """Keep the shared resolver's process cache isolated across fake roots."""

    from frisket.engine._workers import rapidocr_models

    rapidocr_models.rapidocr_model_requirements.cache_clear()
    rapidocr_models.rapidocr_default_model_requirements.cache_clear()
    yield rapidocr_models
    rapidocr_models.rapidocr_default_model_requirements.cache_clear()
    rapidocr_models.rapidocr_model_requirements.cache_clear()


def test_rapidocr_models_require_complete_package_set(
    monkeypatch, tmp_path, rapidocr_model_resolver
):
    """One or two plausible ONNX files are not offline readiness.

    Resolve the version-specific filenames through RapidOCR's own lookup and
    require its complete default detection/classification/recognition trio in
    the package root that a bare ``RapidOCR()`` uses.
    """
    rapidocr_main = pytest.importorskip("rapidocr.main")
    from frisket.ai.models import model_cache

    package_root = tmp_path / "package"
    shared_root = tmp_path / "shared"
    monkeypatch.setattr(rapidocr_main, "root_dir", package_root)
    monkeypatch.setattr(model_cache, "default_cache_root", lambda: shared_root)

    requirements = rapidocr_model_resolver.rapidocr_default_model_requirements()
    assert requirements is not None
    resolved_root, filenames = requirements
    assert resolved_root == package_root / "models"
    assert len(filenames) == len(set(filenames)) == 3

    resolved_root.mkdir(parents=True)
    assert diagnostics._rapidocr_model_source() is None
    for filename in filenames[:-1]:
        (resolved_root / filename).write_bytes(b"model")
    assert diagnostics._rapidocr_model_source() is None
    assert diagnostics._rapidocr_models_present() is False

    (resolved_root / filenames[-1]).write_bytes(b"model")
    assert diagnostics._rapidocr_model_source() == "package"
    assert diagnostics._rapidocr_models_present() is True


def test_rapidocr_models_accept_complete_shared_set_but_not_split_set(
    monkeypatch, tmp_path, rapidocr_model_resolver
):
    """The worker selects one model root; package+shared is not a union."""
    rapidocr_main = pytest.importorskip("rapidocr.main")
    from frisket.ai.models import model_cache

    package_root = tmp_path / "package"
    shared_cache_root = tmp_path / "cache"
    monkeypatch.setattr(rapidocr_main, "root_dir", package_root)
    monkeypatch.setattr(model_cache, "default_cache_root", lambda: shared_cache_root)
    requirements = rapidocr_model_resolver.rapidocr_default_model_requirements()
    assert requirements is not None
    package_models, filenames = requirements
    shared_models = shared_cache_root / "rapidocr"
    package_models.mkdir(parents=True)
    shared_models.mkdir(parents=True)

    for filename in filenames[:-1]:
        (package_models / filename).write_bytes(b"package")
    (shared_models / filenames[-1]).write_bytes(b"shared")
    assert diagnostics._rapidocr_model_source() is None

    for filename in filenames[:-1]:
        (shared_models / filename).write_bytes(b"shared")
    assert diagnostics._rapidocr_model_source() == "shared_cache"
    assert diagnostics._rapidocr_models_present() is True


def test_rapidocr_runtime_report_is_content_free_and_labels_overrides(monkeypatch):
    from frisket.engine._workers import rapidocr_session

    topology = rapidocr_session.RapidOCRTopology(
        effective_cpus=4,
        effective_memory_bytes=8 * 1024**3,
        workers=2,
        onnx_intra_threads=2,
        onnx_inter_threads=1,
        opencv_threads=1,
        memory_mb=3584,
    )
    monkeypatch.setattr(
        rapidocr_session, "rapidocr_topology", lambda expected_rows=None: topology
    )
    for env_name in (
        rapidocr_session._ENV_WORKERS,
        rapidocr_session._ENV_ONNX_THREADS,
        rapidocr_session._ENV_OPENCV_THREADS,
    ):
        monkeypatch.delenv(env_name, raising=False)

    automatic = diagnostics._rapidocr_runtime_report()
    assert automatic == {
        "basis": "multi_row_default",
        "config_mode": "automatic",
        "overrides": [],
        "resolved": True,
        "effective_cpus": 4,
        "effective_memory_bytes": 8 * 1024**3,
        "workers": 2,
        "onnx_intra_threads": 2,
        "onnx_inter_threads": 1,
        "opencv_requested_threads": 1,
        "memory_limit_mb": 3584,
    }

    monkeypatch.setenv(rapidocr_session._ENV_WORKERS, "2")
    overridden = diagnostics._rapidocr_runtime_report()
    assert overridden["config_mode"] == "overridden"
    assert overridden["overrides"] == ["workers"]
    # Raw environment values, paths, and row/model content have no fields in
    # this DTO; only the bounded effective topology crosses Diagnose.
    assert set(overridden) == set(automatic)


def test_hf_snapshot_present_pinned_revision_requires_every_file(tmp_path):
    """Parakeet's artifact resolver pins an exact revision and a fixed file
    allowlist (parakeet_model.py) — the probe must require ALL of them, not
    just a same-named directory (a partial/interrupted download must still
    read as 'fetches on first use', not 'provisioned')."""
    repo, revision, files = "org/repo", "deadbeef", ("a.bin", "b.json")
    assert (
        diagnostics._hf_snapshot_present(
            tmp_path, repo, revision=revision, required_files=files
        )
        is False
    )

    snapshot = tmp_path / "models--org--repo" / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (snapshot / "a.bin").write_bytes(b"1")
    assert (
        diagnostics._hf_snapshot_present(
            tmp_path, repo, revision=revision, required_files=files
        )
        is False
    )  # b.json still missing

    (snapshot / "b.json").write_text("{}")
    assert (
        diagnostics._hf_snapshot_present(
            tmp_path, repo, revision=revision, required_files=files
        )
        is True
    )


def test_hf_snapshot_present_unpinned_accepts_any_prior_snapshot(tmp_path):
    """An hf_snapshot repo probed with no revision (the generic fallback for
    any not-frisket-pinned artifact) accepts any prior snapshot present."""
    repo = "org/some-unpinned-repo"
    assert diagnostics._hf_snapshot_present(tmp_path, repo) is False

    snapshots = tmp_path / "models--org--some-unpinned-repo" / "snapshots"
    snapshots.mkdir(parents=True)
    assert diagnostics._hf_snapshot_present(tmp_path, repo) is False  # empty dir

    empty_snapshot = snapshots / "somehash"
    empty_snapshot.mkdir()
    assert diagnostics._hf_snapshot_present(tmp_path, repo) is False  # no files in it

    (empty_snapshot / "model.bin").write_bytes(b"x")
    assert diagnostics._hf_snapshot_present(tmp_path, repo) is True


def test_engine_provisioned_asr_requires_both_model_and_vad_snapshots(
    monkeypatch, tmp_path
):
    """VAD is on by default in ParakeetAdapter,
    so a real first run needs both the Parakeet model and VAD snapshots —
    the model alone must not read as 'provisioned'."""
    from frisket.engine._workers import parakeet_artifacts as pa

    monkeypatch.setattr(pa, "huggingface_hub_cache", lambda: tmp_path)
    assert diagnostics._engine_provisioned("parakeet") is False

    model_snapshot = (
        tmp_path
        / f"models--{pa.PARAKEET_MODEL_REPO.replace('/', '--')}"
        / "snapshots"
        / pa.PARAKEET_MODEL_REVISION
    )
    model_snapshot.mkdir(parents=True)
    for f in pa.PARAKEET_MODEL_FILES:
        (model_snapshot / f).write_text("x")
    assert diagnostics._engine_provisioned("parakeet") is False  # vad still missing

    vad_snapshot = (
        tmp_path
        / f"models--{pa.PARAKEET_VAD_REPO.replace('/', '--')}"
        / "snapshots"
        / pa.PARAKEET_VAD_REVISION
    )
    vad_snapshot.mkdir(parents=True)
    for f in pa.PARAKEET_VAD_FILES:
        (vad_snapshot / f).write_text("x")
    assert diagnostics._engine_provisioned("parakeet") is True


def test_engine_provisioned_faster_whisper_reads_hf_hub_cache(monkeypatch, tmp_path):
    """faster-whisper's default 'base' size is now pinned (same hf_snapshot
    revision-pin mechanism as Parakeet) — the probe must require the EXACT
    pinned revision + full file list, not just any same-named snapshot dir
    (a partial/wrong-revision cache must still read as 'fetches on first
    use')."""
    from frisket.engine._workers import parakeet_artifacts as pa
    from frisket.ai.models import artifact_manifest

    monkeypatch.setattr(pa, "huggingface_hub_cache", lambda: tmp_path)
    assert diagnostics._engine_provisioned("faster_whisper") is False

    entry = artifact_manifest.whisper_base_artifact()
    assert entry is not None and entry.hf_snapshot is not None
    snap = entry.hf_snapshot

    # A same-repo snapshot under the WRONG revision must not read as
    # provisioned.
    wrong_snapshot = (
        tmp_path
        / f"models--{snap.repo_id.replace('/', '--')}"
        / "snapshots"
        / "somehash"
    )
    wrong_snapshot.mkdir(parents=True)
    for f in snap.files:
        (wrong_snapshot / f).write_bytes(b"x")
    assert diagnostics._engine_provisioned("faster_whisper") is False

    pinned_snapshot = (
        tmp_path
        / f"models--{snap.repo_id.replace('/', '--')}"
        / "snapshots"
        / snap.revision
    )
    pinned_snapshot.mkdir(parents=True)
    assert diagnostics._engine_provisioned("faster_whisper") is False  # files missing
    for f in snap.files:
        (pinned_snapshot / f).write_bytes(b"x")
    assert diagnostics._engine_provisioned("faster_whisper") is True


def test_engine_provisioned_convert_has_no_weights_to_fetch():
    """markitdown does format conversion (no ML weights) — always ready."""
    assert diagnostics._engine_provisioned("convert") is True


def test_engines_report_summary_reflects_provisioned_state(monkeypatch):
    monkeypatch.setattr(
        diagnostics,
        "spacy_model_report",
        lambda: {
            "status": "not_downloaded",
            "summary": "not yet downloaded",
            "ref": "spacy:en_core_web_sm@3.8.0",
        },
    )
    monkeypatch.setattr(diagnostics, "_engine_provisioned", lambda name: False)
    report = diagnostics.engines_report()
    if report["installed"]:
        assert report["provisioned"] == []
        expected_offline = [
            name for name in report["installed"] if name in ("ocr", "faster_whisper")
        ]
        expected_fetches = [
            name
            for name in report["installed"]
            if name not in ("ocr", "faster_whisper")
        ]
        assert report["offline_unavailable"] == expected_offline
        assert report["fetches_on_first_use"] == expected_fetches
        assert "(provisioned)" not in report["summary"]
        assert all(
            (
                "spacy (model not yet downloaded)" in report["summary"]
                if name == "spacy"
                else f"{name} (fetches on first use)" in report["summary"]
            )
            for name in expected_fetches
        )
        if expected_offline:
            if "ocr" in expected_offline:
                assert "ocr (not provisioned; offline worker)" in report["summary"]
            if "faster_whisper" in expected_offline:
                assert (
                    "faster_whisper (not provisioned; pre-populated HF cache required)"
                    in report["summary"]
                )
        # Each installed engine gets the cache/provisioning mechanism it
        # actually uses; there is no one generic Hugging Face remedy.
        if "parakeet" in report["installed"]:
            assert "/api/providers/models/pull" in report["remediation"]
        if "faster_whisper" in report["installed"]:
            assert "HF_HUB_CACHE" in report["remediation"]
            assert "no-network worker sandbox" in report["remediation"]
        if "ocr" in report["installed"]:
            assert "$FRISKET_MODEL_CACHE_DIR/rapidocr" in report["remediation"]
            assert "all three default RapidOCR models" in report["remediation"]
        if "embeddings" in report["installed"]:
            assert "FASTEMBED_CACHE_PATH" in report["remediation"]

    monkeypatch.setattr(diagnostics, "_engine_provisioned", lambda name: True)
    report = diagnostics.engines_report()
    if report["installed"]:
        assert report["fetches_on_first_use"] == []
        assert report["offline_unavailable"] == []
        assert report["provisioned"] == report["installed"]
        assert "(fetches on first use)" not in report["summary"]
        assert report["remediation"] is None


def test_engines_report_includes_runtime_plan_when_rapidocr_is_installed(monkeypatch):
    pytest.importorskip("rapidocr")
    runtime = {
        "basis": "multi_row_default",
        "config_mode": "automatic",
        "overrides": [],
        "resolved": True,
    }
    monkeypatch.setattr(diagnostics, "_rapidocr_runtime_report", lambda: runtime)

    report = diagnostics.engines_report()

    assert "ocr" in report["installed"]
    assert report["rapidocr_runtime"] == runtime


def test_engines_report_uses_product_engine_ids_and_caveats_whisper_probe(
    monkeypatch,
):
    """Transcription engines are labeled with the ids users pick in a run
    spec (ops/transcribe_engines ENGINE_CHOICES: parakeet, faster_whisper), never
    the internal asr/whisper names — and a 'provisioned' faster_whisper
    carries the base-size caveat, because the probe only checks the default
    'base' size while model_size is a per-run knob."""
    monkeypatch.setattr(diagnostics, "_engine_provisioned", lambda name: True)
    report = diagnostics.engines_report()
    assert "asr" not in report["installed"]
    assert "whisper" not in report["installed"]
    if "ocr" in report["installed"]:
        assert "ocr (provisioned: default det/cls/rec)" in report["summary"]
    if "faster_whisper" in report["installed"]:
        assert (
            "faster_whisper (provisioned: base pinned & pullable; "
            "other sizes require pre-populated HF cache)" in report["summary"]
        )


def test_engines_report_rapidocr_only_uses_rapidocr_cache_guidance(monkeypatch):
    pytest.importorskip("rapidocr")
    monkeypatch.setattr(diagnostics, "_engine_provisioned", lambda name: name != "ocr")

    report = diagnostics.engines_report()

    assert report["fetches_on_first_use"] == []
    assert report["offline_unavailable"] == ["ocr"]
    assert "ocr (not provisioned; offline worker)" in report["summary"]
    assert "$FRISKET_MODEL_CACHE_DIR/rapidocr" in report["remediation"]
    assert "HF_HUB_CACHE" not in report["remediation"]
    assert "/api/providers/models/pull" not in report["remediation"]


def test_engines_report_never_raises_when_provisioned_probe_breaks(monkeypatch):
    """A broken presence probe (e.g. a malformed cache dir) degrades one
    engine to its honest unprovisioned class — it must never crash the whole
    report or flip the doctor health check."""

    def _boom(name: str) -> bool:
        raise RuntimeError("simulated presence-probe crash")

    monkeypatch.setattr(diagnostics, "_engine_provisioned", _boom)
    report = diagnostics.engines_report()
    assert report["provisioned"] == []
    assert report["offline_unavailable"] == (
        [name for name in report["installed"] if name in ("ocr", "faster_whisper")]
    )
    assert set(report["fetches_on_first_use"]) == {
        name for name in report["installed"] if name not in ("ocr", "faster_whisper")
    }

    full = diagnostics.run_diagnostics()
    assert "summary" in full["info"]["local_engines"]
    # CORE probes alone decide healthy; local engines is INFO-only.
    assert full["healthy"] is all(c.get("ok") for c in full["core"].values())
