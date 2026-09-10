"""Frisket-owned model-weights cache.

Frisket downloads and OWNS these model bytes on disk
(Ollama owns its own blob store; parakeet/faster-whisper delegate to the HF
library cache). It therefore needs a frisket-owned model dir that did not
exist before.

Resolution mirrors the managed-runtime cache
(``frisket.plugins.managed_runtime.default_cache_root``):

    FRISKET_MODEL_CACHE_DIR                     (explicit override)
      else $XDG_CACHE_HOME/frisket/models
      else ~/.cache/frisket/models

Layout under it:

    <model_dir>/
      opus-mt/<src>-<tgt>/            # ct2_pair artifacts (model.bin, *.spm, …)
      hf/<owner>/<repo>/<revision>/…  # hf_file artifacts (GGUF, onnx, …)
      spacy/<package>/<version>/…     # pinned pipeline wheel + derived model
      .pull-tmp/<pull_id>/            # in-flight downloads; atomic-rename on done

Downloads land in ``.pull-tmp/<pull_id>/`` and are ``os.replace``d into place
only AFTER checksum verification, so a crashed/cancelled pull never leaves a
half-file the runtime mistakes for installed (the analogue of Ollama's own
atomicity). Ollama models are unaffected -- they stay in Ollama's own store.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid a jobs<->models runtime import cycle
    from frisket.engine.jobs.artifact_ref import ArtifactRef

_CACHE_ENV_VAR = "FRISKET_MODEL_CACHE_DIR"
_TMP_DIRNAME = ".pull-tmp"


def default_cache_root() -> Path:
    """The user-level frisket model-weights dir. Env-overridable via
    ``FRISKET_MODEL_CACHE_DIR``; otherwise ``$XDG_CACHE_HOME/frisket/models``
    or ``~/.cache/frisket/models``. Never inside a project workspace/bundle."""
    override = os.environ.get(_CACHE_ENV_VAR)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "frisket" / "models"


def _relative_dir(ref: "ArtifactRef") -> Path:
    """The install directory RELATIVE to the model root, by scheme."""
    if ref.scheme == "opus-mt":
        assert ref.pair is not None
        return Path("opus-mt") / ref.pair
    if ref.scheme == "hf":
        assert ref.hf_repo is not None and ref.hf_revision is not None
        assert ref.hf_path is not None
        owner, name = ref.hf_repo.split("/", 1)
        # The hf artifact's own repo-relative path may itself have parent dirs;
        # the file's dir is included so a multi-dir repo layout is preserved.
        return Path("hf") / owner / name / ref.hf_revision
    if ref.scheme == "spacy":
        assert ref.spacy_model is not None and ref.spacy_version is not None
        return Path("spacy") / ref.spacy_model / ref.spacy_version
    raise ValueError(f"scheme {ref.scheme!r} has no frisket-owned cache dir")


def artifact_install_dir(ref: "ArtifactRef", *, root: Path | None = None) -> Path:
    """Absolute directory the artifact's files live under once installed."""
    base = root if root is not None else default_cache_root()
    return base / _relative_dir(ref)


def hf_install_path(ref: "ArtifactRef", *, root: Path | None = None) -> Path:
    """Absolute path of the single hf file once installed (its repo-relative
    path resolved under the revision dir)."""
    assert ref.scheme == "hf" and ref.hf_path is not None
    return artifact_install_dir(ref, root=root) / ref.hf_path


def tmp_dir(pull_id: int | str, *, root: Path | None = None) -> Path:
    """The in-flight download staging dir for a pull, under ``.pull-tmp``."""
    base = root if root is not None else default_cache_root()
    return base / _TMP_DIRNAME / str(pull_id)


def is_installed(
    ref: "ArtifactRef",
    expected_files: list[tuple[str, int]],
    *,
    root: Path | None = None,
) -> bool:
    """Cheap presence + size probe; a full per-row re-hash is too slow.
    ``expected_files`` is ``[(repo_relpath, size), …]`` from the
    manifest. Every file must exist at its expected size."""
    install_dir = artifact_install_dir(ref, root=root)
    for relpath, size in expected_files:
        target = install_dir / relpath
        if not target.is_file():
            return False
        if size >= 0 and target.stat().st_size != size:
            return False
    return True


def _prune_empty_parents(leaf: Path, stop: Path) -> None:
    """Remove now-empty parent dirs from ``leaf`` up to (but not including)
    ``stop`` -- so uninstalling one hf file does not leave empty owner/repo/rev
    scaffolding, while never touching a dir that still holds a sibling."""
    current = leaf
    stop = stop.resolve()
    while current != current.parent:
        current = current.parent
        try:
            if current.resolve() == stop or current.resolve() == stop.parent:
                break
        except OSError:
            break
        try:
            current.rmdir()  # only succeeds when empty -> siblings are safe
        except OSError:
            break


def promote(
    pull_id: int | str, ref: "ArtifactRef", *, root: Path | None = None
) -> Path:
    """Move a completed staging download into its install location, crash-safe
    and scoped to this artifact only.

    - ``hf`` (single-file artifact): the file is ``os.replace``d into place --
      one atomic syscall that overwrites the prior version and never touches a
      sibling file sharing the owner/repo/revision directory.
    - Directory artifacts (``opus-mt`` pairs and ``spacy`` wheels): the
      verified staging tree is parked next to the destination, then the swap
      moves the CURRENT install aside to a ``.old`` name and moves the new tree
      in, removing ``.old`` last. The prior install's bytes are never destroyed
      before the replacement is in place -- a crash mid-swap leaves the prior
      recoverable at ``dest`` or the ``.old`` sibling, never a half-written dir
      at ``dest``.
    Returns the final install dir.
    """
    staging = tmp_dir(pull_id, root=root)

    if ref.scheme == "hf":
        assert ref.hf_path is not None
        dest_file = hf_install_path(ref, root=root)
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging / ref.hf_path, dest_file)  # atomic single-file swap
        discard_tmp(pull_id, root=root)
        return artifact_install_dir(ref, root=root)

    # Directory artifact (Opus-MT pair or spaCy wheel): stage-then-swap.
    dest = artifact_install_dir(ref, root=root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staged_final = dest.with_name(f"{dest.name}.new-{pull_id}")
    if staged_final.exists():
        shutil.rmtree(staged_final)
    os.replace(staging, staged_final)  # verified tree parked beside dest
    old: Path | None = None
    if dest.exists():
        old = dest.with_name(f"{dest.name}.old-{pull_id}")
        if old.exists():
            shutil.rmtree(old)
        os.replace(dest, old)  # move current aside (prior preserved in .old)
    os.replace(staged_final, dest)  # new tree into place
    if old is not None and old.exists():
        shutil.rmtree(old, ignore_errors=True)
    return dest


def discard_tmp(pull_id: int | str, *, root: Path | None = None) -> None:
    """Remove a pull's staging dir (cancel/failure cleanup). Idempotent."""
    staging = tmp_dir(pull_id, root=root)
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)


def uninstall(ref: "ArtifactRef", *, root: Path | None = None) -> bool:
    """Delete an installed artifact's bytes, scoped to the exact ref. An
    ``hf`` uninstall removes only that one file (siblings sharing the
    revision dir survive), pruning empty parents; an ``opus-mt`` uninstall
    removes the pair's own dir. Returns whether anything was removed."""
    base = root if root is not None else default_cache_root()
    if ref.scheme == "hf":
        target = hf_install_path(ref, root=root)
        if target.is_file():
            target.unlink()
            _prune_empty_parents(target, base / "hf")
            return True
        return False
    install_dir = artifact_install_dir(ref, root=root)
    if install_dir.exists():
        shutil.rmtree(install_dir, ignore_errors=True)
        return True
    return False


__all__ = [
    "artifact_install_dir",
    "default_cache_root",
    "discard_tmp",
    "hf_install_path",
    "is_installed",
    "promote",
    "tmp_dir",
    "uninstall",
]
