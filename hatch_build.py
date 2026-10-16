"""Hatch hook that compiles and packages SQLite Vec1 for the build host."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


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
            build_data["infer_tag"] = True
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
