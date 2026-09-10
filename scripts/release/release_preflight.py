#!/usr/bin/env python3
"""Owner-safe release preflight for the public frisket package names."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARTIFACT_DIR = Path("/tmp/frisket-release-preflight-dist")
OWNER_CONFIRM_PHRASE = "owner-account-frisket"
PYPI_JSON_URL = "https://pypi.org/pypi/frisket/json"
FRONTEND_HOST_PACKAGE_NAME = "@frisket/frontend-host"
NPM_REGISTRY_URL = "https://registry.npmjs.org/@frisket%2Ffrontend-host"
DIRECT_REQUIREMENT_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?\s*@\s*\S+")


def load_pyproject(root: Path) -> dict[str, Any]:
    with (root / "pyproject.toml").open("rb") as file:
        return tomllib.load(file)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def package_metadata_findings(root: Path = ROOT) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    project = load_pyproject(root).get("project", {})

    if project.get("name") != "frisket-data":
        errors.append("pyproject.toml [project].name must be 'frisket-data'")
    if project.get("description") in {None, "", "Add your description here"}:
        errors.append("pyproject.toml [project].description is still a placeholder")
    if project.get("license") != "Apache-2.0":
        errors.append("pyproject.toml [project].license must be Apache-2.0")

    readme = project.get("readme")
    if not isinstance(readme, str) or not (root / readme).is_file():
        errors.append("pyproject.toml [project].readme must point at an existing file")
    elif not (root / readme).read_text().strip():
        errors.append(f"{readme} must not be empty for a public PyPI page")

    if not (root / "LICENSE").is_file():
        errors.append("LICENSE file is required before PyPI publication")

    scripts = project.get("scripts", {})
    if scripts.get("frisket") != "frisket.cli:main":
        errors.append("pyproject.toml must expose frisket = 'frisket.cli:main'")

    urls = project.get("urls", {})
    for key in ("Homepage", "Repository", "Issues"):
        if not urls.get(key):
            errors.append(f"pyproject.toml [project.urls].{key} is required")

    classifiers = set(project.get("classifiers", []))
    if "Programming Language :: Python :: 3.12" not in classifiers:
        errors.append("PyPI classifiers should include Python 3.12")

    requirements = list(project.get("dependencies", []))
    for extra_requirements in project.get("optional-dependencies", {}).values():
        requirements.extend(extra_requirements)
    direct_packages = sorted(
        {
            match.group(1)
            for requirement in requirements
            if (match := DIRECT_REQUIREMENT_RE.match(str(requirement))) is not None
        }
    )
    if direct_packages:
        errors.append(
            "PyPI rejects direct-URL requirements; replace metadata URLs for: "
            + ", ".join(direct_packages)
        )

    web_package_path = root / "web" / "package.json"
    has_frontend_host_package = False
    if web_package_path.is_file():
        web_package = load_json(web_package_path)
        if web_package.get("name") == "frisket":
            errors.append(
                "web/package.json must not be reused as the public npm package"
            )
        if web_package.get("name") == FRONTEND_HOST_PACKAGE_NAME:
            has_frontend_host_package = True
        elif web_package.get("private") is not True:
            errors.append(
                "web/package.json must stay private unless it is the explicit "
                f"{FRONTEND_HOST_PACKAGE_NAME} package"
            )
    else:
        warnings.append("web/package.json is missing; npm collision check skipped")

    root_npm_package = root / "package.json"
    if root_npm_package.exists():
        root_package = load_json(root_npm_package)
        if (
            root_package.get("name") == "frisket"
            and root_package.get("private") is not True
        ):
            errors.append(
                "root package.json would publish as frisket; create a clear npm package first"
            )

    if not has_frontend_host_package:
        warnings.append(
            f"no explicit {FRONTEND_HOST_PACKAGE_NAME} package found; "
            "keep npm publish blocked"
        )

    return errors, warnings


def print_findings(errors: list[str], warnings: list[str]) -> None:
    if errors:
        print("package metadata: FAIL")
        for error in errors:
            print(f"ERROR: {error}")
    else:
        print("package metadata: OK")
    for warning in warnings:
        print(f"WARN: {warning}")


def run_command(cmd: list[str]) -> int:
    print("+ " + " ".join(cmd))
    return subprocess.run(cmd, cwd=ROOT, text=True).returncode


def check_url_status(name: str, url: str, timeout: float) -> int:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "frisket-release-preflight/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            print(
                f"{name}: claimed or present (HTTP {response.status}); verify owner control manually"
            )
            return 0
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"{name}: unclaimed or unavailable to anonymous lookup (HTTP 404)")
            return 0
        print(f"{name}: registry lookup failed with HTTP {exc.code}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"{name}: registry lookup failed: {exc.reason}", file=sys.stderr)
        return 1


def artifact_paths(artifact_dir: Path, version: str) -> list[Path]:
    if not artifact_dir.exists():
        return []
    prefix = f"frisket-{version}"
    return sorted(
        path
        for path in artifact_dir.iterdir()
        if path.name.startswith(prefix)
        and (path.suffix == ".whl" or path.name.endswith(".tar.gz"))
    )


def handle_pypi_publish(args: argparse.Namespace) -> int:
    if args.confirm_owner_account != OWNER_CONFIRM_PHRASE:
        print(
            "refusing PyPI publish: pass "
            f"--confirm-owner-account {OWNER_CONFIRM_PHRASE!r} only when using owner accounts",
            file=sys.stderr,
        )
        return 2

    version = str(load_pyproject(ROOT).get("project", {}).get("version", ""))
    artifacts = artifact_paths(args.artifact_dir, version)
    if not artifacts:
        print(
            f"refusing PyPI publish: no frisket {version} artifacts in {args.artifact_dir}; run --build first",
            file=sys.stderr,
        )
        return 2

    cmd = ["uv", "publish", *[str(path) for path in artifacts]]
    if not args.execute_upload:
        print("dry-run: add --execute-upload to actually upload to PyPI")
        print("+ " + " ".join(cmd))
        return 0

    if shutil.which("uv") is None:
        print("refusing PyPI publish: uv is not installed", file=sys.stderr)
        return 2
    return run_command(cmd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build",
        action="store_true",
        help="run uv build into --artifact-dir after static checks pass",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=DEFAULT_ARTIFACT_DIR,
        help="directory for built distributions (default: /tmp/frisket-release-preflight-dist)",
    )
    parser.add_argument(
        "--skip-web-build",
        action="store_true",
        help="with --build, assume web/dist is already built + staged into "
        "src/frisket/web_static/ (skip npm build/stage)",
    )
    parser.add_argument(
        "--check-availability",
        action="store_true",
        help="perform unauthenticated PyPI/npm HTTP lookups; uses network, no secrets",
    )
    parser.add_argument(
        "--registry-timeout",
        type=float,
        default=10.0,
        help="seconds per registry lookup when --check-availability is used",
    )
    parser.add_argument(
        "--publish-pypi",
        action="store_true",
        help="plan or execute a PyPI upload from --artifact-dir",
    )
    parser.add_argument(
        "--publish-npm",
        action="store_true",
        help="always refused until a purposeful public npm package exists",
    )
    parser.add_argument(
        "--confirm-owner-account",
        help=f"required phrase for publish planning: {OWNER_CONFIRM_PHRASE}",
    )
    parser.add_argument(
        "--execute-upload",
        action="store_true",
        help="actually run upload commands after owner-account confirmation",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    errors, warnings = package_metadata_findings()
    print_findings(errors, warnings)
    if errors:
        return 1

    if args.check_availability:
        pypi_status = check_url_status(
            "PyPI frisket", PYPI_JSON_URL, args.registry_timeout
        )
        npm_status = check_url_status(
            f"npm {FRONTEND_HOST_PACKAGE_NAME}",
            NPM_REGISTRY_URL,
            args.registry_timeout,
        )
        if pypi_status or npm_status:
            return 1

    if args.build:
        if shutil.which("uv") is None:
            print("uv is required for --build", file=sys.stderr)
            return 2
        # uv_build has no frontend hook; stage the SPA before building the wheel.
        # --skip-web-build is valid only when the caller already staged it.
        if not args.skip_web_build:
            from build_frontend import stage_web_assets

            try:
                stage_web_assets()
            except (SystemExit, subprocess.CalledProcessError) as exc:
                print(f"web asset staging failed: {exc}", file=sys.stderr)
                return 1
        build_status = run_command(["uv", "build", "--out-dir", str(args.artifact_dir)])
        if build_status:
            return build_status

    if args.publish_npm:
        print(
            "refusing npm publish: @frisket/frontend-host has no registry release "
            "workflow; consume its exact-SHA npm pack artifact",
            file=sys.stderr,
        )
        return 2

    if args.publish_pypi:
        return handle_pypi_publish(args)

    if args.execute_upload:
        print("--execute-upload has no effect without a publish flag", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
