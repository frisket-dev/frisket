"""Validate and install one data-only managed-worker sandbox policy.

The desktop/runtime bootstrap loads this file by absolute path before importing
application or domain code. Keep imports in this module and its two sibling
helpers in the standard library.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


POLICY_VERSION = 1
FENCE_UNAVAILABLE_MARKER = "FRISKET_SANDBOX_FENCE_UNAVAILABLE:"
_POLICY_KEYS = frozenset({"version", "audit_netwall", "fence"})
_FENCE_KEYS = frozenset(
    {"allow_unix_sockets", "read", "write", "allow_exec", "python_roots"}
)


def _load_sibling(name: str) -> ModuleType:
    path = Path(__file__).with_name(f"{name}.py").resolve()
    spec = importlib.util.spec_from_file_location(f"_frisket_sandbox_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load sandbox helper {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _require_keys(value: dict[str, Any], expected: frozenset[str], name: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"invalid {name} keys (missing={missing!r}, extra={extra!r})")


def _paths(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"sandbox policy {name} must be a list of absolute paths")
    if any(not os.path.isabs(item) for item in value):
        raise ValueError(f"sandbox policy {name} paths must be absolute")
    return tuple(value)


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"sandbox policy {name} must be a boolean")
    return value


def _validated(policy: object) -> tuple[bool, tuple[Any, ...] | None]:
    if not isinstance(policy, dict):
        raise ValueError("sandbox policy must be an object")
    _require_keys(policy, _POLICY_KEYS, "sandbox policy")
    if type(policy["version"]) is not int or policy["version"] != POLICY_VERSION:
        raise ValueError(f"sandbox policy version must be {POLICY_VERSION}")
    audit_netwall = _boolean(policy["audit_netwall"], "audit_netwall")
    raw_fence = policy["fence"]
    if raw_fence is None:
        return audit_netwall, None
    if not isinstance(raw_fence, dict):
        raise ValueError("sandbox policy fence must be an object or null")
    _require_keys(raw_fence, _FENCE_KEYS, "sandbox policy fence")
    return audit_netwall, (
        _boolean(raw_fence["allow_unix_sockets"], "fence.allow_unix_sockets"),
        _paths(raw_fence["read"], "fence.read"),
        _paths(raw_fence["write"], "fence.write"),
        _boolean(raw_fence["allow_exec"], "fence.allow_exec"),
        _boolean(raw_fence["python_roots"], "fence.python_roots"),
    )


def install(policy: object) -> None:
    """Install the policy before the runtime bootstrap imports domain code."""

    audit_netwall, fence_args = _validated(policy)
    child_fence = _load_sibling("_child_fence") if fence_args is not None else None
    netwall = _load_sibling("_netwall") if audit_netwall else None
    try:
        if child_fence is not None:
            child_fence._frisket_fence_install(*fence_args)
        if netwall is not None:
            netwall.install()
    except BaseException as exc:
        sys.stderr.write(f"{FENCE_UNAVAILABLE_MARKER} {type(exc).__name__}: {exc}\n")
        sys.stderr.flush()
        os._exit(126)
