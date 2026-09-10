"""Dependency contract for Frisket's MCP 1.x transport boundary."""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MCP_REQUIREMENT = "mcp>=1.27.2,<2"


def _project_dependencies() -> list[str]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["project"]["dependencies"]


def _locked_mcp_version() -> str:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    matches = [package for package in lock["package"] if package["name"] == "mcp"]
    assert len(matches) == 1, f"expected one locked MCP package, found {matches!r}"
    return matches[0]["version"]


def _major_minor(version: str) -> tuple[int, int]:
    major, minor, *_ = version.split(".")
    return int(major), int(minor)


def test_mcp_dependency_stays_on_the_supported_sdk_major() -> None:
    """Fresh resolution must not cross the intentionally unmigrated 2.x API."""
    assert [
        dependency
        for dependency in _project_dependencies()
        if dependency == "mcp"
        or dependency.startswith("mcp<")
        or dependency.startswith("mcp>")
    ] == [MCP_REQUIREMENT]

    locked_version = _locked_mcp_version()
    installed_version = importlib.metadata.version("mcp")
    assert (1, 27) <= _major_minor(locked_version) < (2, 0)
    assert (1, 27) <= _major_minor(installed_version) < (2, 0)


def test_supported_mcp_transport_imports_remain_available() -> None:
    from mcp.server.fastmcp import FastMCP
    from mcp.shared.memory import create_connected_server_and_client_session

    assert FastMCP is not None
    assert callable(create_connected_server_and_client_session)
