"""Hatch hook that compiles and packages SQLite Vec1 for the build host."""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from packaging.tags import parse_tag


_MANYLINUX_TAG = re.compile(r"manylinux_[0-9]+_[0-9]+_(?:x86_64|aarch64)")


def _host_platform_tag() -> str:
    """Return the build host platform without claiming portable Linux ABI."""

    # build_vec1.py emits one arm64 dylib with a macOS 14 deployment target.
    # setup-python's sysconfig tag can instead describe the interpreter
    # (including universal2 or its own deployment target), which is not the
    # binary this hook puts in the wheel.
    if sys.platform == "darwin" and platform.machine().lower() in {
        "arm64",
        "aarch64",
    }:
        return "macosx_14_0_arm64"
    return sysconfig.get_platform().replace("-", "_").replace(".", "_")


def _wheel_platform_tag() -> str:
    """Select a conservative host tag or a CI-verified manylinux override."""

    platform_tag = os.environ.get("FRISKET_WHEEL_PLATFORM_TAG")
    if platform_tag is None:
        return _host_platform_tag()
    if _MANYLINUX_TAG.fullmatch(platform_tag) is None:
        raise RuntimeError(
            "FRISKET_WHEEL_PLATFORM_TAG is reserved for verified manylinux builds"
        )
    parsed = parse_tag(f"py3-none-{platform_tag}")
    if len(parsed) != 1:
        raise RuntimeError(f"invalid Frisket wheel platform override: {platform_tag}")
    host = _host_platform_tag()
    if not host.startswith("linux_") or not platform_tag.endswith(host[5:]):
        raise RuntimeError(
            f"manylinux override {platform_tag} does not match build host {host}"
        )
    return platform_tag


class CustomBuildHook(BuildHookInterface):
    """Build the pinned extension and force it into a platform wheel."""

    _temporary: tempfile.TemporaryDirectory[str] | None = None

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name != "wheel" or version != "standard":
            return

        output_directory = Path(self.directory)
        output_directory.mkdir(parents=True, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(
            prefix=".frisket-vec1-", dir=output_directory
        )
        native_root = Path(self._temporary.name)
        command = [
            sys.executable,
            str(Path(self.root) / "scripts" / "build_vec1.py"),
            "--output-root",
            str(native_root),
        ]
        try:
            subprocess.run(command, cwd=self.root, check=True)
            libraries = [path for path in native_root.glob("*/*") if path.is_file()]
            if len(libraries) != 1:
                raise RuntimeError(
                    "Vec1 build must produce exactly one target library; "
                    f"found {len(libraries)}"
                )
            library = libraries[0]
            member = PurePosixPath("frisket", "retrieval", "_native").joinpath(
                *library.relative_to(native_root).parts
            )
            build_data["force_include"][str(library)] = str(member)
            build_data["pure_python"] = False
            # Release Linux wheels set this only inside a pinned manylinux
            # image and verify the result with auditwheel. Ordinary builds use
            # a generic host tag and never infer manylinux from host glibc.
            platform_tag = _wheel_platform_tag()
            build_data["tag"] = f"py3-none-{platform_tag}"
            print(f"Vec1 wheel platform: {platform_tag}")
        except BaseException:
            self._cleanup()
            raise

    def finalize(
        self,
        version: str,
        build_data: dict[str, Any],
        artifact_path: str,
    ) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
