"""Isolated, stdlib-only entrypoint; application imports happen after policy."""

from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
import runpy
import sys


# Application-owned entrypoints, never module names supplied by a request.
TARGETS = {
    "parakeet-session": ("frisket.engine._workers.parakeet_worker", "framed_main"),
    "rapidocr-session": ("frisket.engine._workers.rapidocr_worker", "framed_main"),
    "parakeet-artifacts": (
        "frisket.engine._workers.parakeet_artifacts_worker",
        "main",
    ),
    "faster-whisper": ("frisket.engine._workers.faster_whisper_worker", "main"),
    "tesseract": ("frisket.engine._workers.tesseract_worker", "main"),
    "markitdown": ("frisket.engine._workers.markitdown_worker", "main"),
    "faces": ("frisket.engine._workers.faces_worker", "main"),
    "pypdf": ("frisket.engine._workers.pypdf_worker", "main"),
    "plugin": ("frisket.plugins.subprocess_runner", "main"),
    "recipe": ("frisket.engine.sandbox._recipe_worker", "main"),
    "cli-worker": ("frisket.cli", "worker"),
}
KINDS = frozenset(TARGETS) | {"runtime-info", "yt-dlp", "native-exec"}


def main() -> int:
    arguments = sys.argv[1:]
    policy = None
    if arguments[:1] == ["--policy"]:
        if len(arguments) < 3:
            raise ValueError("missing worker policy")
        policy = json.loads(arguments[1])
        if not isinstance(policy, dict):
            raise ValueError("worker policy must be an object")
        arguments = arguments[2:]
    if not arguments or arguments[0] not in KINDS:
        raise ValueError("unknown worker kind")
    kind, *args = arguments
    app_root = Path(__file__).resolve().parents[2]
    # -I ignores cwd, PYTHONPATH, and the user site. Keep this interpreter's
    # installed dependencies, and load only this bundle's application code.
    sys.path.insert(0, str(app_root))
    if policy is not None:
        policy_file = app_root / "frisket/engine/sandbox/_bootstrap_policy.py"
        spec = importlib.util.spec_from_file_location(
            "_frisket_worker_policy", policy_file
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("worker policy installer unavailable")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        installer.install(policy)
    sys.argv = [kind, *args]
    if kind == "runtime-info":
        print(
            json.dumps(
                {
                    "executable": sys.executable,
                    "prefix": sys.prefix,
                    "app_code_root": str(app_root),
                }
            )
        )
        return 0
    if kind == "yt-dlp":
        runpy.run_module("yt_dlp", run_name="__main__")
        return 0
    if kind == "native-exec":
        import os

        if not policy or not (policy.get("fence") or {}).get("allow_exec"):
            raise ValueError("native worker requires an executable confinement policy")
        if not args or not Path(args[0]).is_absolute():
            raise ValueError("native worker requires an absolute executable")
        os.execv(args[0], args)
    module, function = TARGETS[kind]
    handler = getattr(importlib.import_module(module), function)
    result = handler(args) if kind == "cli-worker" else handler()
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
