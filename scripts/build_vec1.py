#!/usr/bin/env python3
"""Build the pinned SQLite Vec1 loadable extension for the current platform."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "vendor" / "sqlite-vec1" / "vec1.c"
INCLUDE = SOURCE.parent
PINNED_SOURCES = {
    "vec1.c": "4f9a83efd78992769ba7a8e630d755b93370258387694498f22d1eb3cfd3e766",
    "sqlite3.h": "cd20a2877bc82b94e36c1093b727dd87cfa3c1d581aac6e240925535cbd90166",
    "sqlite3ext.h": "48963abfefb41ac329ef9d706acb5eadba952a99a748285c9ded6b5e0942721b",
}
DEFAULT_OUTPUT_ROOT = ROOT / "src" / "frisket" / "retrieval" / "_native"


class BuildError(RuntimeError):
    """The pinned source or native compiler could not produce Vec1."""


def _target() -> tuple[str, str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        architecture = "x86_64"
    elif machine in {"arm64", "aarch64"}:
        architecture = "aarch64"
    else:
        raise BuildError(f"unsupported Vec1 architecture: {machine or 'unknown'}")

    if system == "linux":
        return f"linux-{architecture}", "vec1.so", "unix"
    if system == "darwin" and architecture == "aarch64":
        return "macos-arm64", "vec1.dylib", "macos"
    if system == "windows" and architecture == "x86_64":
        return "windows-x86_64", "vec1.dll", "windows"
    raise BuildError(f"unsupported Vec1 target: {system or 'unknown'}-{architecture}")


def _run(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError as exc:
        raise BuildError(f"native compiler not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise BuildError(
            f"Vec1 compiler failed with exit code {exc.returncode}"
        ) from exc


def _compile_unix(source: Path, output: Path, workspace: Path, *, macos: bool) -> None:
    compiler = os.environ.get("CC", "clang" if macos else "cc")
    common = ["-O3", "-DNDEBUG", "-fPIC", "-pthread", "-I", str(INCLUDE)]
    if macos:
        common.extend(["-arch", "arm64", "-mmacosx-version-min=14.0"])

    if platform.machine().lower() in {"amd64", "x86_64"}:
        scalar = workspace / "vec1-scalar.o"
        avx2 = workspace / "vec1-avx2.o"
        _run(
            [
                compiler,
                *common,
                "-DVEC1SIMD=SCALAR",
                "-c",
                str(source),
                "-o",
                str(scalar),
            ]
        )
        _run(
            [
                compiler,
                *common,
                "-DVEC1SIMD=AVX2",
                "-mavx2",
                "-mfma",
                "-c",
                str(source),
                "-o",
                str(avx2),
            ]
        )
        objects = [scalar, avx2]
    else:
        native = workspace / "vec1.o"
        _run([compiler, *common, "-c", str(source), "-o", str(native)])
        objects = [native]

    link_mode = "-dynamiclib" if macos else "-shared"
    link = [compiler, link_mode, *(str(item) for item in objects), "-pthread"]
    if not macos:
        link.extend(["-Wl,-z,relro,-z,now"])
    link.extend(["-lm", "-o", str(output)])
    _run(link)


def _compile_windows(source: Path, output: Path, workspace: Path) -> None:
    compiler = os.environ.get("CC", "cl")
    scalar = workspace / "vec1-scalar.obj"
    avx2 = workspace / "vec1-avx2.obj"
    common = ["/nologo", "/O2", "/DNDEBUG", "/W4", f"/I{INCLUDE}"]
    _run(
        [
            compiler,
            *common,
            "/c",
            "/DVEC1SIMD=SCALAR",
            str(source),
            f"/Fo{scalar}",
        ]
    )
    _run(
        [
            compiler,
            *common,
            "/c",
            "/DVEC1SIMD=AVX2",
            "/arch:AVX2",
            str(source),
            f"/Fo{avx2}",
        ]
    )
    _run(
        [
            "link",
            "/NOLOGO",
            "/DLL",
            f"/OUT:{output}",
            str(scalar),
            str(avx2),
        ]
    )


def build(output_root: Path = DEFAULT_OUTPUT_ROOT) -> Path:
    for filename, expected in PINNED_SOURCES.items():
        actual = hashlib.sha256((INCLUDE / filename).read_bytes()).hexdigest()
        if actual != expected:
            raise BuildError(
                f"vendored SQLite {filename} hash mismatch: "
                f"expected {expected}, got {actual}"
            )

    target, filename, toolchain = _target()
    destination = output_root.resolve() / target / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="frisket-vec1-build-") as temporary:
        workspace = Path(temporary)
        staged = workspace / filename
        if toolchain == "windows":
            _compile_windows(SOURCE, staged, workspace)
        else:
            _compile_unix(SOURCE, staged, workspace, macos=toolchain == "macos")
        if not staged.is_file() or staged.stat().st_size == 0:
            raise BuildError("native compiler produced no Vec1 library")
        temporary_destination = destination.with_suffix(destination.suffix + ".tmp")
        shutil.copyfile(staged, temporary_destination)
        temporary_destination.chmod(0o755)
        temporary_destination.replace(destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="directory under which the target-specific library is written",
    )
    args = parser.parse_args()
    try:
        output = build(args.output_root)
    except (BuildError, OSError) as exc:
        print(f"build_vec1: {exc}", file=sys.stderr)
        return 1
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(f"{output} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
