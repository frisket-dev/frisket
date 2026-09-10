#!/usr/bin/env python3
"""Fail-closed checks for the public Python wheel release artifact.

This verifier intentionally inspects only wheel bytes. It does not resolve or
install dependencies, import the wheel, or contact a package index; the release
job performs those runtime checks separately in a fresh environment with
``probe_installed_release.py``.
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import urlsplit


HASHED_ASSET = re.compile(r".+-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$")
# The public wheel ships exactly one top-level package (``frisket``) plus its
# own dist-info/data directories. Anything else at the top level is an
# unexpected package that should never have been bundled into this artifact.
EXPECTED_TOP_LEVEL_ROOTS = ("frisket",)


class _IndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.has_html_element = False
        self.has_app_root = False
        self.asset_references: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "html":
            self.has_html_element = True
        values = dict(attrs)
        if values.get("id") == "root":
            self.has_app_root = True
        for attribute in ("src", "href"):
            value = values.get(attribute)
            if value:
                path = urlsplit(value).path.lstrip("/")
                if path.startswith("assets/"):
                    self.asset_references.add(path)


def _inspect_index(archive: zipfile.ZipFile, static_index: str) -> list[str]:
    failures: list[str] = []
    try:
        contents = archive.read(static_index)
    except KeyError:
        return [f"missing required packaged SPA entry: {static_index}"]
    if not contents.strip():
        return [f"packaged SPA index is empty: {static_index}"]
    try:
        html = contents.decode("utf-8")
    except UnicodeDecodeError:
        return [f"packaged SPA index is not UTF-8 HTML: {static_index}"]

    parser = _IndexParser()
    parser.feed(html)
    parser.close()
    if not parser.has_html_element:
        failures.append(f"packaged SPA index has no <html> element: {static_index}")
    if not parser.has_app_root:
        failures.append(
            f'packaged SPA index has no id="root" app mount: {static_index}'
        )

    hashed_references = sorted(
        reference
        for reference in parser.asset_references
        if HASHED_ASSET.fullmatch(PurePosixPath(reference).name)
    )
    if not hashed_references:
        failures.append(
            f"packaged SPA index references no hashed assets: {static_index}"
        )
        return failures

    unhashed_references = sorted(parser.asset_references - set(hashed_references))
    if unhashed_references:
        failures.append(
            "packaged SPA index references unhashed assets: "
            + ", ".join(unhashed_references)
        )

    for reference in sorted(parser.asset_references):
        packaged_path = f"frisket/web_static/{reference}"
        try:
            asset = archive.read(packaged_path)
        except KeyError:
            failures.append(
                f"packaged SPA index references missing asset: {packaged_path}"
            )
            continue
        if not asset:
            failures.append(
                f"packaged SPA index references empty asset: {packaged_path}"
            )
    return failures


def _unexpected_package_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts:
        return False
    root = parts[0].lower()
    return not any(
        root == expected or root.startswith(f"{expected}-")
        for expected in EXPECTED_TOP_LEVEL_ROOTS
    )


def verify_wheel(path: str) -> list[str]:
    """Return human-readable failures for ``path``; an empty list means pass."""
    failures: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            static_index = "frisket/web_static/index.html"
            failures.extend(_inspect_index(archive, static_index))
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"cannot read wheel {path}: {exc}"]

    unexpected_paths = sorted(name for name in names if _unexpected_package_path(name))
    if unexpected_paths:
        failures.append(
            "unexpected package paths present: " + ", ".join(unexpected_paths)
        )

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", help="path to the wheel to verify")
    args = parser.parse_args(argv)
    failures = verify_wheel(args.wheel)
    if failures:
        for failure in failures:
            print(f"release artifact: FAIL: {failure}", file=sys.stderr)
        return 1
    print(f"release artifact: OK: {args.wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
