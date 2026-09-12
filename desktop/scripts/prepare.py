#!/usr/bin/env python3
"""Build the ordinary Frisket wheel and stage the macOS desktop resources."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path

DESKTOP = Path(__file__).resolve().parents[1]
ROOT = DESKTOP.parent
RESOURCES = DESKTOP / "resources"
PYTHON_VERSION = "3.12.13"
UV_VERSION = "0.11.29"

# These are executable package resources, not an inventory of every source file.
WHEEL_RESOURCES = (
    "frisket/web_static/index.html",
    "frisket/runtime/_bootstrap.py",
    "frisket/runtime/_guard.py",
    "frisket/data/plugin_sdk.mjs",
    "frisket/data/action_ui.d.ts",
    "frisket/data/first_party_workbench_descriptors.json",
    "frisket/data/public_suffix_list.dat",
    "frisket/data/face_detection/face_detection_yunet_2023mar.onnx",
    "frisket/data/face_detection/LICENSE",
    "frisket/authoring/bundled_plugins/frisket.geo/frontend/plugin.js",
    "frisket/authoring/bundled_plugins/frisket.transliterate/plugin.json",
    "frisket/authoring/bundled_plugins/frisket.opencorporates/plugin.json",
    "frisket/authoring/bundled_plugins/frisket.ftm/plugin.json",
    "frisket/ai/llm/model_catalog.json",
    "frisket/ai/llm/pricing_data.json",
)


def run(*args: str, quiet: bool = False) -> None:
    subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL if quiet else None,
    )


def desktop_version(version: str) -> str:
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+))?", version)
    if match is None:
        raise ValueError(f"Unsupported desktop release version: {version}")
    release, phase, number = match.groups()
    if phase:
        return f"{release}-{dict(a='alpha', b='beta', rc='rc')[phase]}.{number}"
    return release


def stage_native(name: str, asset: dict, cache: Path) -> None:
    archive_path = cache / f"{name}-{asset['sha256']}"
    if not archive_path.is_file():
        temporary = archive_path.with_suffix(".download")
        try:
            request = urllib.request.Request(
                asset["url"], headers={"User-Agent": "Frisket-desktop-build"}
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                with temporary.open("wb") as output:
                    shutil.copyfileobj(response, output)
            temporary.replace(archive_path)
        finally:
            temporary.unlink(missing_ok=True)
    with archive_path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != asset["sha256"]:
        archive_path.unlink()
        raise ValueError(f"Native asset checksum mismatch: {name}")
    destination = RESOURCES / "bin" / name
    if asset["url"].endswith(".zip"):
        with zipfile.ZipFile(archive_path) as archive:
            with archive.open(asset["member"]) as source:
                with destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
    else:
        with tarfile.open(archive_path) as archive:
            member = archive.getmember(asset["member"])
            if not member.isfile():
                raise ValueError(f"Native archive member is not a file: {name}")
            with archive.extractfile(member) as source:
                with destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
    destination.chmod(0o755)


def prepare(*, skip_web_build: bool = False) -> None:
    actual_uv = subprocess.check_output(["uv", "--version"], text=True).split()[1]
    if actual_uv != UV_VERSION:
        raise SystemExit(f"Desktop builds require uv {UV_VERSION}; found {actual_uv}")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    playwright = next(
        p["version"] for p in lock["package"] if p["name"] == "playwright"
    )
    if not skip_web_build:
        run("python3", "scripts/release/build_frontend.py")
    if not (ROOT / "src/frisket/web_static/index.html").is_file():
        raise SystemExit(
            "Build and stage the local web UI before packaging the desktop app"
        )

    # Only generated resources are replaced; user data lives outside the bundle.
    if RESOURCES.exists():
        shutil.rmtree(RESOURCES)
    (RESOURCES / "bin").mkdir(parents=True)
    code_root = RESOURCES / "python"
    code_root.mkdir()
    with tempfile.TemporaryDirectory(prefix="frisket-desktop-wheel-") as temporary:
        run("uv", "build", "--wheel", "--out-dir", temporary)
        (wheel,) = Path(temporary).glob("*.whl")
        with zipfile.ZipFile(wheel) as archive:
            missing = sorted(set(WHEEL_RESOURCES) - set(archive.namelist()))
            if missing:
                raise ValueError(
                    f"Desktop wheel is missing runtime resources: {missing}"
                )
            archive.extractall(code_root)

    requirements = RESOURCES / "requirements.txt"
    run(
        "uv",
        "export",
        "--frozen",
        "--no-dev",
        "--extra",
        "standard",
        "--no-emit-project",
        "--no-header",
        "--no-annotate",
        "--output-file",
        str(requirements),
        quiet=True,
    )
    manifest = {
        "schema": 1,
        "pythonVersion": PYTHON_VERSION,
        "uvVersion": UV_VERSION,
        "requirementsSha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
        "playwrightVersion": playwright,
    }
    (RESOURCES / "runtime.json").write_text(json.dumps(manifest, indent=2) + "\n")
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    (RESOURCES / "build.json").write_text(
        json.dumps(
            {
                "version": desktop_version(project["project"]["version"]),
                "pythonPackageVersion": project["project"]["version"],
                "revision": revision,
            },
            indent=2,
        )
        + "\n"
    )

    native = json.loads((DESKTOP / "native-assets.json").read_text())
    cache = DESKTOP / "build" / "downloads"
    cache.mkdir(parents=True, exist_ok=True)
    for name, asset in native.items():
        print(f"Staging {name} {asset['version']}", flush=True)
        stage_native(name, asset, cache)
    shutil.copy2(DESKTOP / "native-assets.json", RESOURCES / "native-assets.json")
    shutil.copy2(DESKTOP / "THIRD_PARTY.md", RESOURCES / "THIRD_PARTY.md")
    shutil.copytree(DESKTOP / "licenses", RESOURCES / "licenses")
    print(f"Desktop resources ready: {RESOURCES}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-web-build", action="store_true")
    args = parser.parse_args()
    prepare(skip_web_build=args.skip_web_build)
