"""Pinned artifact resolution for the local Parakeet worker.

Resolution intentionally happens in a short-lived supervised child.  The
inference child receives only immutable, explicit snapshot directories and is
offline; it never owns first-use downloads or ambient Hugging Face credentials.
This module imports neither :mod:`onnx_asr` nor :mod:`onnxruntime`.

The model + VAD identity (repo id, revision, file list) is resolved from
``frisket.models.artifact_manifest``, whose two Parakeet entries
(``kind="hf_snapshot"``, ``integrity == "hf_revision_pinned"``) are
themselves BUILT from the ``parakeet_model.py`` constants -- so routing the
lookup through the manifest keeps this resolver and the pull surface on one
shared pin, but the pin's edit site is ``parakeet_model.py``, not the
manifest (the inference worker validates against those same constants and
rejects any other revision). Only the IDENTITY lookup is routed through the
manifest; the download mechanism here (a short-lived sandboxed child running
``snapshot_download``) and its parent-side re-validation
(``_validate_snapshot``) are unchanged -- the manifest's ``hf_snapshot``
source type is deliberately thin pass-through data for exactly this existing
mechanism, not a new one.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frisket.ai.models import artifact_manifest
from frisket.ai.models.artifact_manifest import (
    PARAKEET_MODEL_HF_REPO as PARAKEET_MODEL_REPO,  # noqa: F401 - re-exported
    PARAKEET_VAD_HF_REPO as PARAKEET_VAD_REPO,  # noqa: F401 - re-exported
)
from frisket.engine.sandbox.shim import (
    SandboxPolicy,
    SandboxTeardownError,
    run_sandboxed,
)

from .parakeet_model import (
    MODEL as PARAKEET_MODEL,  # noqa: F401 - re-exported for importers
    MODEL_FILES as PARAKEET_MODEL_FILES,  # noqa: F401 - re-exported for importers
    MODEL_REVISION as PARAKEET_MODEL_REVISION,
    VAD_FILES as PARAKEET_VAD_FILES,  # noqa: F401 - re-exported for importers
    VAD_REVISION as PARAKEET_VAD_REVISION,  # noqa: F401 - re-exported for importers
)

PARAKEET_VAD_MODEL = "silero"

ARTIFACT_WALL_SECONDS = 1_800

# A static repo-owned body keeps the resolver launch auditable.  Network access
# is deliberately enabled only for this short-lived child; inference uses the
# separate offline/netwalled bootstrap in parakeet_session.py.
PARAKEET_RESOLVER_BOOTSTRAP = "from frisket.engine._workers.parakeet_artifacts import resolver_main\nresolver_main()"


class ParakeetArtifactUnavailable(RuntimeError):
    """Pinned artifacts could not be resolved and verified."""


class ParakeetArtifactCancelled(RuntimeError):
    """Operator cancellation stopped artifact resolution."""


@dataclass(frozen=True)
class ParakeetArtifacts:
    cache_dir: Path
    model_path: Path
    model_revision: str = PARAKEET_MODEL_REVISION
    vad_path: Path | None = None
    vad_revision: str | None = None


def huggingface_hub_cache() -> Path:
    """Return the parent-selected persistent Hub cache directory.

    ``HF_HUB_CACHE`` is the canonical boundary.  The older variables are read
    only to preserve existing local installs; the inference child is always
    given one explicit ``HF_HUB_CACHE`` and never receives these ambient
    variables.
    """

    explicit = os.environ.get("HF_HUB_CACHE")
    if explicit:
        return Path(explicit).expanduser().absolute()
    legacy = os.environ.get("HUGGINGFACE_HUB_CACHE")
    if legacy:
        return Path(legacy).expanduser().absolute()
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return (Path(hf_home).expanduser() / "hub").absolute()
    return (Path.home() / ".cache" / "huggingface" / "hub").absolute()


def _snapshot_payload(
    *, repo_id: str, revision: str, files: tuple[str, ...], cache_dir: Path
) -> dict[str, Any]:
    return {
        "repo_id": repo_id,
        "revision": revision,
        "files": list(files),
        "cache_dir": str(cache_dir),
    }


def _resolve_snapshot(payload: dict[str, Any]) -> str:
    """Resolver-child implementation; imports the Hub client lazily."""

    from huggingface_hub import snapshot_download

    repo_id = str(payload["repo_id"])
    revision = str(payload["revision"])
    files = tuple(str(value) for value in payload["files"])
    cache_dir = Path(str(payload["cache_dir"])).expanduser().absolute()
    snapshot = Path(
        snapshot_download(
            repo_id,
            revision=revision,
            cache_dir=cache_dir,
            allow_patterns=list(files),
            token=False,
            max_workers=4,
        )
    ).absolute()
    return str(
        _validate_snapshot(
            snapshot,
            cache_dir=cache_dir,
            repo_id=repo_id,
            revision=revision,
            files=files,
        )
    )


def _validate_snapshot(
    snapshot: Path,
    *,
    cache_dir: Path,
    repo_id: str,
    revision: str,
    files: tuple[str, ...],
) -> Path:
    try:
        snapshot = snapshot.expanduser().resolve(strict=True)
        cache_dir = cache_dir.expanduser().resolve(strict=True)
    except OSError as error:
        raise ParakeetArtifactUnavailable(
            "resolver returned an unavailable snapshot"
        ) from error
    if not snapshot.is_dir() or snapshot.name != revision:
        raise ParakeetArtifactUnavailable("resolver returned an invalid snapshot")
    repo_cache_name = f"models--{repo_id.replace('/', '--')}"
    expected_relative = Path(repo_cache_name) / "snapshots" / revision
    try:
        relative_snapshot = snapshot.relative_to(cache_dir)
    except ValueError as error:
        raise ParakeetArtifactUnavailable(
            "resolver returned a snapshot outside the configured cache"
        ) from error
    if relative_snapshot != expected_relative:
        raise ParakeetArtifactUnavailable(
            "resolver returned a snapshot for the wrong repository"
        )
    repo_cache = cache_dir / repo_cache_name
    for filename in files:
        # Filenames are Frisket-owned constants, never child/user input.  Keep
        # validation lexical because Hub snapshot entries are symlinks into its
        # content-addressed ``blobs`` directory.
        if Path(filename).is_absolute() or ".." in Path(filename).parts:
            raise ParakeetArtifactUnavailable("artifact allowlist is invalid")
        artifact = snapshot / filename
        if not artifact.is_file():
            raise ParakeetArtifactUnavailable(
                "a required file is missing from the pinned snapshot"
            )
        try:
            artifact.resolve(strict=True).relative_to(repo_cache)
        except (OSError, ValueError) as error:
            raise ParakeetArtifactUnavailable(
                "a required file resolves outside the configured cache"
            ) from error
    return snapshot


def resolver_main() -> None:
    """Resolve one or two pinned snapshots and print one bounded JSON result."""

    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise TypeError("resolver payload must be an object")
        model = _resolve_snapshot(dict(payload["model"]))
        vad_payload = payload.get("vad")
        vad = _resolve_snapshot(dict(vad_payload)) if vad_payload else None
        result: dict[str, Any] = {"ok": True, "model_path": model}
        if vad is not None:
            result["vad_path"] = vad
    except Exception:  # child details and paths never cross this boundary
        result = {"ok": False, "error": "pinned artifacts are unavailable"}
    sys.stdout.write(json.dumps(result, allow_nan=False, separators=(",", ":")))
    sys.stdout.flush()


def _manifest_snapshot_identity(
    getter: Any, label: str
) -> tuple[str, str, tuple[str, ...]]:
    """Resolve an ``hf_snapshot`` manifest entry's (repo_id, revision, files)
    identity. The shipped catalog always has the Parakeet model + VAD entries
    (added alongside the ``hf_snapshot`` source type); a miss here means the
    manifest was edited to drop one -- fail loudly rather than silently
    falling back to a value the manifest no longer vouches for."""
    entry = getter()
    if entry is None or entry.hf_snapshot is None:
        raise ParakeetArtifactUnavailable(
            f"the pinned {label} manifest entry is missing"
        )
    snapshot = entry.hf_snapshot
    return snapshot.repo_id, snapshot.revision, snapshot.files


async def resolve_parakeet_artifacts(
    *,
    vad: bool,
    should_cancel: Any = None,
) -> ParakeetArtifacts:
    """Resolve and verify the immutable model snapshots in a bounded child.

    The repo/revision/file-list identity for both the model and (if
    requested) the VAD companion is looked up from the pinned manifest on
    every call so this resolver and the explicit pull surface share one pin.
    The manifest entry is NOT the re-pinning edit site, though: it is built
    from the ``parakeet_model.py`` constants, and the inference worker
    validates against those same constants (rejecting any other revision) --
    an editor re-pins in ``parakeet_model.py``.
    """

    cache_dir = huggingface_hub_cache()
    model_repo, model_revision, model_files = _manifest_snapshot_identity(
        artifact_manifest.parakeet_model_artifact, "Parakeet model"
    )
    payload: dict[str, Any] = {
        "model": _snapshot_payload(
            repo_id=model_repo,
            revision=model_revision,
            files=model_files,
            cache_dir=cache_dir,
        )
    }
    if vad:
        vad_repo, vad_revision, vad_files = _manifest_snapshot_identity(
            artifact_manifest.parakeet_vad_artifact, "Parakeet VAD"
        )
        payload["vad"] = _snapshot_payload(
            repo_id=vad_repo,
            revision=vad_revision,
            files=vad_files,
            cache_dir=cache_dir,
        )

    try:
        result = await run_sandboxed(
            [sys.executable, "-c", PARAKEET_RESOLVER_BOOTSTRAP],
            policy=SandboxPolicy(
                cpu_seconds=ARTIFACT_WALL_SECONDS,
                wall_seconds=ARTIFACT_WALL_SECONDS,
                memory_mb=1024,
                allow_network=True,
                env_passthrough=["VIRTUAL_ENV", "PYTHONPATH"],
            ),
            stdin_data=json.dumps(payload, separators=(",", ":")).encode(),
            should_cancel=should_cancel,
        )
    except (asyncio.CancelledError, SandboxTeardownError):
        raise
    except Exception as error:
        raise ParakeetArtifactUnavailable(
            "pinned artifact resolution could not be started"
        ) from error
    if result.cancelled:
        raise ParakeetArtifactCancelled("artifact resolution was cancelled")
    if not result.ok:
        raise ParakeetArtifactUnavailable("pinned artifact resolution failed")
    try:
        body = json.loads(result.stdout)
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise ValueError("resolver did not return success")
        expected_fields = {"ok", "model_path"}
        if vad:
            expected_fields.add("vad_path")
        if set(body) != expected_fields:
            raise ValueError("resolver returned unknown or missing fields")
        model_path = _validate_snapshot(
            Path(str(body["model_path"])),
            cache_dir=cache_dir,
            repo_id=model_repo,
            revision=model_revision,
            files=model_files,
        )
        vad_path = None
        if vad:
            vad_path = _validate_snapshot(
                Path(str(body["vad_path"])),
                cache_dir=cache_dir,
                repo_id=vad_repo,
                revision=vad_revision,
                files=vad_files,
            )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ParakeetArtifactUnavailable(
            "artifact resolver returned an invalid result"
        ) from error
    return ParakeetArtifacts(
        cache_dir=cache_dir.resolve(strict=True),
        model_path=model_path,
        model_revision=model_revision,
        vad_path=vad_path,
        vad_revision=vad_revision if vad else None,
    )
