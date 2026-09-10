#!/usr/bin/env python3
"""Build the web UI and stage it inside the Python package for the wheel.

`frisket <workspace>` is meant to be one command: a `pip`/`uvx` install must
carry the built SPA so the local server can serve it (onboard-local-ui-serving-v1).
uv_build (our build backend, pyproject.toml [build-system]) has no frontend
build hook, so this script is the explicit "build the frontend first" step the
release flow runs before `uv build`:

    1. `npm --prefix web run build`  ->  web/dist/<edition>/ (build-editions.mjs
       builds every edition; the wheel serves the LOCAL tier)
    2. copy web/dist/local  ->  src/frisket/web_static/  (gitignored build artifact)

At runtime `frisket.server.static_serving.packaged_static_dir()` reads
`frisket/web_static/index.html` from the installed package. In a dev checkout
this directory is absent, which is the dev-mode signal (vite serves the UI).

`scripts/release/release_preflight.py --build` calls `stage_web_assets()` before
`uv build`; run this module directly to stage assets by hand.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "web"
WEB_DIST = WEB_DIR / "dist"
# build-editions.mjs writes one bundle per edition under web/dist/; the wheel
# ships the local tier's UI (the team tier serves from the image's own copy).
LOCAL_EDITION_DIST = WEB_DIST / "local"
PACKAGED_STATIC = ROOT / "src" / "frisket" / "web_static"


def build_web(*, skip_npm_build: bool = False) -> None:
    """Run `npm --prefix web run build` unless the caller already built it."""
    if skip_npm_build:
        return
    subprocess.run(
        ["npm", "--prefix", str(WEB_DIR), "run", "build"],
        cwd=ROOT,
        check=True,
    )


def stage_web_assets(*, skip_npm_build: bool = False) -> Path:
    """Build web/dist (unless skipped) and copy it into the package tree.

    Returns the staged directory. Raises if the build produced no
    index.html — a silently-empty static dir would ship a headless wheel.
    """
    build_web(skip_npm_build=skip_npm_build)
    if not (LOCAL_EDITION_DIST / "index.html").is_file():
        raise SystemExit(
            f"web build produced no index.html at {LOCAL_EDITION_DIST}; "
            "run `npm --prefix web run build` first"
        )
    if PACKAGED_STATIC.exists():
        shutil.rmtree(PACKAGED_STATIC)
    shutil.copytree(LOCAL_EDITION_DIST, PACKAGED_STATIC)
    return PACKAGED_STATIC


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-npm-build",
        action="store_true",
        help="assume web/dist is already built; only copy it into the package",
    )
    args = parser.parse_args(argv)
    staged = stage_web_assets(skip_npm_build=args.skip_npm_build)
    count = sum(1 for _ in staged.rglob("*") if _.is_file())
    print(f"staged {count} web asset file(s) into {staged}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
