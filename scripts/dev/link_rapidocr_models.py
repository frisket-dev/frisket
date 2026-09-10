#!/usr/bin/env python3
"""Seed/link the machine-shared RapidOCR model cache (the
env-gap section).

rapidocr>=3.8.1's wheel does not ship its default-config onnx models under
the filenames its own ``default_models.yaml`` looks up, so a fresh venv's
first ``RapidOCR()`` construction reaches out to modelscope.cn — a hard
failure in a sandboxed/offline dev environment, and repeated per venv on a
machine with several worktrees (this repo has many).

``frisket.ops.ocr_engines.OcrEngines._ocr_rapidocr`` already reads a shared cache dir
directly (RapidOCR's own ``Global.model_root_dir`` config key) whenever the
venv's own bundled models are absent, so OCR runs offline the moment that
cache is populated — this script is the convenience step that populates it,
plus (optionally) makes the CURRENT venv's own ``rapidocr/models`` dir
"present" too, for tooling that checks that path directly (frisket's own
``rapidocr_models_present`` probe, the source-extra gap-marked acceptance
node in ``tests/ops/test_ocr_runtime_availability.py``).

Idempotent, offline, no network calls:

  seed   copy any of the current venv's own real (non-symlink) onnx/dict
         model files into the shared cache, skipping names already there
  link   symlink any shared-cache file missing from the current venv's own
         ``rapidocr/models`` dir into place (never overwrites a real file)

Usage:
    python scripts/dev/link_rapidocr_models.py            # seed only
    python scripts/dev/link_rapidocr_models.py --link     # seed + link

A no-op run before any venv on the machine has ever downloaded real models
(there is nothing to seed) is not a failure — the shared cache just stays
empty until some venv, or CI's cached artifact, provisions it.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def _venv_models_dir() -> Path | None:
    try:
        import rapidocr
    except ImportError:
        return None
    return Path(rapidocr.__file__).resolve().parent / "models"


def _shared_cache_dir() -> Path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from frisket.ops.ocr_engines import rapidocr_shared_model_cache_dir

    return rapidocr_shared_model_cache_dir()


def seed(venv_models: Path, shared: Path) -> list[str]:
    """Copy real files the current venv has into the shared cache, skipping
    names already present there. Returns the filenames copied."""
    shared.mkdir(parents=True, exist_ok=True)
    copied = []
    for item in sorted(venv_models.iterdir()):
        if not item.is_file() or item.is_symlink():
            continue
        dest = shared / item.name
        if dest.exists():
            continue
        shutil.copy2(item, dest)
        copied.append(item.name)
    return copied


def link(venv_models: Path, shared: Path) -> list[str]:
    """Symlink shared-cache files missing from the current venv's own models
    dir into place. Never touches a name the venv already has (real file or
    otherwise) — seeding only ever ADDS names, it never overwrites."""
    linked = []
    if not shared.is_dir():
        return linked
    for item in sorted(shared.iterdir()):
        if not item.is_file():
            continue
        dest = venv_models / item.name
        if dest.exists() or dest.is_symlink():
            continue
        dest.symlink_to(item)
        linked.append(item.name)
    return linked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--link",
        action="store_true",
        help="also symlink shared-cache files into the current venv's own "
        "rapidocr/models dir",
    )
    args = parser.parse_args()

    venv_models = _venv_models_dir()
    shared = _shared_cache_dir()
    print(f"shared cache: {shared}")

    if venv_models is None:
        print("rapidocr not installed in this venv — nothing to seed")
    else:
        print(f"venv models:  {venv_models}")
        copied = seed(venv_models, shared)
        if copied:
            print(f"seeded {len(copied)} file(s) into the shared cache: {copied}")
        else:
            print("nothing new to seed (venv has nothing the cache lacks)")

    if args.link:
        if venv_models is None:
            print("--link requested but rapidocr is not installed — skipping")
        else:
            linked = link(venv_models, shared)
            if linked:
                print(f"linked {len(linked)} file(s) into the venv: {linked}")
            else:
                print("nothing new to link (venv already has everything cached)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
