"""Runtime-managed spaCy pipeline wheel.

The model is a pinned artifact, not a Python dependency.  The shared model
pull worker owns download/progress/checksum/provenance; this module owns the
read side: report its state, verify the pin before use, and derive a loadable
pipeline directory from the verified wheel without modifying site-packages.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import stat
import uuid
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from filelock import FileLock

from frisket.ai.models import artifact_manifest, model_cache
from frisket.engine.jobs.artifact_ref import normalize_artifact_ref

MODEL_NAME = "en_core_web_sm"
MODEL_VERSION = "3.8.0"
WHEEL_FILENAME = "en_core_web_sm-3.8.0-py3-none-any.whl"


class SpacyModelUnavailable(RuntimeError):
    """The pinned pipeline is absent or failed integrity verification."""


@dataclass(frozen=True)
class SpacyModelState:
    status: Literal["present", "not_downloaded", "hash_mismatch"]
    source: Literal["managed_cache", "python_package"] | None
    ref: str
    detail: str

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


def _pinned():
    pinned = artifact_manifest.spacy_model_artifact()
    if pinned is None or len(pinned.files) != 1:
        raise RuntimeError("the pinned spaCy model manifest entry is missing")
    return pinned


def _artifact_ref():
    return normalize_artifact_ref(artifact_manifest.SPACY_MODEL_REF)


def install_dir(*, cache_root: Path | None = None) -> Path:
    return model_cache.artifact_install_dir(_artifact_ref(), root=cache_root)


def wheel_path(*, cache_root: Path | None = None) -> Path:
    return install_dir(cache_root=cache_root) / WHEEL_FILENAME


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def model_state(
    *, cache_root: Path | None = None, include_python_package: bool = True
) -> SpacyModelState:
    """Return the bounded doctor/runtime state without importing spaCy.

    A managed wheel takes precedence over an ad-hoc package install: if bytes
    exist at Frisket's owned path but fail the pin, that corruption is reported
    and never silently masked by a second copy in site-packages.
    """
    pinned = _pinned()
    pinned_file = pinned.files[0]
    path = wheel_path(cache_root=cache_root)
    if path.exists():
        try:
            matches = (
                path.is_file()
                and path.stat().st_size == pinned_file.size
                and _sha256(path) == pinned_file.sha256
            )
        except OSError:
            matches = False
        if not matches:
            return SpacyModelState(
                status="hash_mismatch",
                source="managed_cache",
                ref=pinned.ref,
                detail=(
                    "the cached spaCy model does not match Frisket's pinned "
                    "SHA256; remove it and download the model again"
                ),
            )
        return SpacyModelState(
            status="present",
            source="managed_cache",
            ref=pinned.ref,
            detail="the pinned spaCy model is present and checksum-verified",
        )

    if include_python_package and importlib.util.find_spec(MODEL_NAME) is not None:
        return SpacyModelState(
            status="present",
            source="python_package",
            ref=pinned.ref,
            detail="a manually installed spaCy model package is present",
        )
    return SpacyModelState(
        status="not_downloaded",
        source=None,
        ref=pinned.ref,
        detail=(
            "the pinned spaCy model has not been downloaded; use the model "
            "download control or POST /api/providers/models/pull"
        ),
    )


def _model_path(runtime_root: Path) -> Path:
    return runtime_root / MODEL_NAME / f"{MODEL_NAME}-{MODEL_VERSION}"


def _complete_model_path(runtime_root: Path) -> Path | None:
    path = _model_path(runtime_root)
    if (path / "config.cfg").is_file() and (path / "meta.json").is_file():
        return path
    return None


def ensure_loadable_model_path(*, cache_root: Path | None = None) -> Path:
    """Verify and extract the managed wheel, returning a spaCy data path.

    The wheel is re-hashed before every process's first load. Extraction is a
    derived cache under the same exact artifact directory and is serialized by
    a file lock; a crash leaves only a disposable dot-prefixed staging tree.
    """
    state = model_state(cache_root=cache_root, include_python_package=False)
    if state.status != "present":
        raise SpacyModelUnavailable(state.detail)

    root = install_dir(cache_root=cache_root)
    runtime_root = root / "runtime"
    ready = _complete_model_path(runtime_root)
    if ready is not None:
        return ready

    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / ".extract.lock")):
        ready = _complete_model_path(runtime_root)
        if ready is not None:
            return ready

        staging = root / f".runtime-{uuid.uuid4().hex}"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            with zipfile.ZipFile(wheel_path(cache_root=cache_root)) as archive:
                for member in archive.infolist():
                    rel = PurePosixPath(member.filename)
                    if not rel.parts or rel.parts[0] != MODEL_NAME:
                        continue
                    if rel.is_absolute() or any(
                        part in {"", ".", ".."} for part in rel.parts
                    ):
                        raise SpacyModelUnavailable(
                            "the pinned spaCy wheel contains an unsafe path"
                        )
                    mode = member.external_attr >> 16
                    if mode and stat.S_ISLNK(mode):
                        raise SpacyModelUnavailable(
                            "the pinned spaCy wheel contains an unsupported symlink"
                        )
                    target = staging.joinpath(*rel.parts)
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)

            ready = _complete_model_path(staging)
            if ready is None:
                raise SpacyModelUnavailable(
                    "the pinned spaCy wheel does not contain a loadable pipeline"
                )
            if runtime_root.exists():
                shutil.rmtree(runtime_root)
            os.replace(staging, runtime_root)
        except (OSError, zipfile.BadZipFile) as exc:
            raise SpacyModelUnavailable(
                "the pinned spaCy wheel could not be prepared for loading"
            ) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    ready = _complete_model_path(runtime_root)
    if ready is None:  # defensive: promotion must never yield a partial model
        raise SpacyModelUnavailable(
            "the pinned spaCy model extraction did not complete"
        )
    return ready


__all__ = [
    "MODEL_NAME",
    "MODEL_VERSION",
    "SpacyModelState",
    "SpacyModelUnavailable",
    "ensure_loadable_model_path",
    "install_dir",
    "model_state",
    "wheel_path",
]
