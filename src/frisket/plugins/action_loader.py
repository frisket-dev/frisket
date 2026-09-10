"""Persistent, package-qualified Python imports for trusted installed Actions.

Installed plugins execute with the same process authority as native Actions.
The namespace isolates Python module names; it is not a security sandbox.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.machinery
import sys
import threading
from pathlib import Path
from types import ModuleType


_IMPORT_LOCK = threading.RLock()


def load_action_module(
    plugin_root: Path, module_path: Path, *, package_identity: str = ""
) -> ModuleType:
    root = plugin_root.resolve()
    relative = module_path.resolve().relative_to(root)
    if relative.suffix != ".py" or any(
        not part.isidentifier() for part in relative.with_suffix("").parts
    ):
        raise ValueError("plugin module must be a package-relative Python module")
    # Each admitted package version has ordinary sys.modules lifetime. No
    # sys.path insertion, per-invocation unload, or mutation of builtin Actions.
    namespace = (
        "_frisket_plugin_"
        + hashlib.sha256(f"{root}\0{package_identity}".encode()).hexdigest()
    )
    with _IMPORT_LOCK:
        if namespace not in sys.modules:
            package = ModuleType(namespace)
            package.__path__ = [str(root)]
            package.__package__ = namespace
            package.__spec__ = importlib.machinery.ModuleSpec(
                namespace, loader=None, is_package=True
            )
            sys.modules[namespace] = package
        return importlib.import_module(
            namespace + "." + ".".join(relative.with_suffix("").parts)
        )
