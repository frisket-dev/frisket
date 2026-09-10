#!/usr/bin/env python3
"""Probe an installed Frisket wheel from outside its source checkout.

The caller owns environment creation, wheel installation, console-script
checks, and ``frisket doctor``. This script proves import provenance and the
packaged SPA's ASGI behavior using the installed runtime dependencies.
"""

from __future__ import annotations

import argparse
import re
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

import frisket
from frisket.server.app import create_app
from frisket.server.static_serving import packaged_static_dir


HASHED_ASSET = re.compile(r".+-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$")


class ReleaseProbeError(RuntimeError):
    pass


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.has_app_root = False
        self.assets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id") == "root":
            self.has_app_root = True
        for attribute in ("src", "href"):
            value = values.get(attribute)
            if not value:
                continue
            path = urlsplit(value).path
            if path.startswith("/assets/") and HASHED_ASSET.fullmatch(
                PurePosixPath(path).name
            ):
                self.assets.append(path)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseProbeError(message)


def _assert_installed_import(checkout_root: Path) -> Path:
    package_path = Path(frisket.__file__).resolve()
    _assert(
        not package_path.is_relative_to(checkout_root.resolve()),
        f"frisket imported from source checkout instead of installed wheel: {package_path}",
    )
    _assert(
        "site-packages" in package_path.parts,
        f"frisket import is not under site-packages: {package_path}",
    )
    return package_path


def run_probe(*, checkout_root: Path, workspace: Path) -> None:
    package_path = _assert_installed_import(checkout_root)
    static_dir = packaged_static_dir()
    _assert(static_dir is not None, "installed wheel has no packaged static directory")
    _assert(
        static_dir.resolve().is_relative_to(package_path.parent),
        f"packaged static directory is outside installed package: {static_dir}",
    )
    workspace.mkdir(parents=True, exist_ok=True)
    app = create_app(workspace, serve_spa=True, enable_provider_config=False)
    with TestClient(app) as client:
        root = client.get("/")
        _assert(root.status_code == 200, f"GET / returned {root.status_code}")
        _assert(
            root.headers.get("content-type", "").startswith("text/html"),
            f"GET / content type is not HTML: {root.headers.get('content-type')}",
        )
        parser = _AssetParser()
        parser.feed(root.text)
        parser.close()
        _assert(parser.has_app_root, 'GET / HTML has no id="root" app mount')
        _assert(bool(parser.assets), "GET / HTML references no hashed asset")

        asset_path = parser.assets[0]
        asset = client.get(asset_path)
        _assert(
            asset.status_code == 200,
            f"GET {asset_path} returned {asset.status_code}",
        )
        _assert(bool(asset.content), f"GET {asset_path} returned an empty body")
        _assert(
            asset.headers.get("cache-control") == "public, max-age=31536000, immutable",
            f"GET {asset_path} has wrong cache policy: "
            f"{asset.headers.get('cache-control')}",
        )

        browser_route = client.get("/p/release-proof/s/sheet")
        _assert(
            browser_route.status_code == 200,
            f"SPA browser route returned {browser_route.status_code}",
        )
        _assert(
            browser_route.headers.get("content-type", "").startswith("text/html"),
            "SPA browser route did not return HTML",
        )

        api_miss = client.get("/api/release-proof-not-a-route")
        _assert(
            api_miss.status_code == 404,
            f"unmatched API route returned {api_miss.status_code}",
        )
        _assert(
            api_miss.headers.get("content-type", "").startswith("application/json"),
            "unmatched API route did not return JSON",
        )
        _assert(
            api_miss.json() == {"detail": "Not Found"},
            f"unmatched API route returned unexpected payload: {api_miss.text}",
        )

    print(f"installed release probe: OK: {package_path} (static: {static_dir})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout-root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args(argv)
    run_probe(checkout_root=args.checkout_root, workspace=args.workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
