from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts.release import probe_installed_release
from scripts.release.verify_release_artifact import verify_wheel


INDEX = b"""<!doctype html>
<html><head><script type="module" src="/assets/index-AbCd1234.js"></script></head>
<body><div id="root"></div></body></html>
"""
ASSET = b"console.log('frisket')"


def _wheel(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)
    return path


def _valid_files() -> dict[str, bytes]:
    return {
        "frisket/__init__.py": b"",
        "frisket/web_static/index.html": INDEX,
        "frisket/web_static/assets/index-AbCd1234.js": ASSET,
    }


def test_verify_release_wheel_requires_packaged_spa(tmp_path):
    wheel = _wheel(tmp_path / "frisket.whl", {"frisket/__init__.py": b""})

    assert verify_wheel(str(wheel)) == [
        "missing required packaged SPA entry: frisket/web_static/index.html"
    ]


def test_verify_release_wheel_rejects_empty_index(tmp_path):
    files = _valid_files()
    files["frisket/web_static/index.html"] = b""
    wheel = _wheel(tmp_path / "frisket.whl", files)

    assert verify_wheel(str(wheel)) == [
        "packaged SPA index is empty: frisket/web_static/index.html"
    ]


def test_verify_release_wheel_rejects_fake_index_without_html_or_root(tmp_path):
    files = _valid_files()
    files["frisket/web_static/index.html"] = b"artifact"
    wheel = _wheel(tmp_path / "frisket.whl", files)

    failures = verify_wheel(str(wheel))

    assert "packaged SPA index has no <html> element" in failures[0]
    assert any('no id="root" app mount' in failure for failure in failures)
    assert any("references no hashed assets" in failure for failure in failures)


def test_verify_release_wheel_rejects_index_without_app_root(tmp_path):
    files = _valid_files()
    files["frisket/web_static/index.html"] = INDEX.replace(b'id="root"', b'id="app"')
    wheel = _wheel(tmp_path / "frisket.whl", files)

    assert verify_wheel(str(wheel)) == [
        'packaged SPA index has no id="root" app mount: frisket/web_static/index.html'
    ]


def test_verify_release_wheel_rejects_missing_referenced_asset(tmp_path):
    files = _valid_files()
    del files["frisket/web_static/assets/index-AbCd1234.js"]
    wheel = _wheel(tmp_path / "frisket.whl", files)

    assert verify_wheel(str(wheel)) == [
        "packaged SPA index references missing asset: "
        "frisket/web_static/assets/index-AbCd1234.js"
    ]


def test_verify_release_wheel_rejects_empty_referenced_asset(tmp_path):
    files = _valid_files()
    files["frisket/web_static/assets/index-AbCd1234.js"] = b""
    wheel = _wheel(tmp_path / "frisket.whl", files)

    assert verify_wheel(str(wheel)) == [
        "packaged SPA index references empty asset: "
        "frisket/web_static/assets/index-AbCd1234.js"
    ]


def test_verify_release_wheel_rejects_unhashed_asset_reference(tmp_path):
    files = _valid_files()
    files["frisket/web_static/index.html"] = INDEX.replace(
        b"index-AbCd1234.js", b"index.js"
    )
    files["frisket/web_static/assets/index.js"] = ASSET
    wheel = _wheel(tmp_path / "frisket.whl", files)

    assert verify_wheel(str(wheel)) == [
        "packaged SPA index references no hashed assets: frisket/web_static/index.html"
    ]


def test_verify_release_wheel_rejects_unexpected_package_paths(tmp_path):
    files = _valid_files()
    files["other_package/__init__.py"] = b""
    wheel = _wheel(tmp_path / "frisket.whl", files)

    assert verify_wheel(str(wheel)) == [
        "unexpected package paths present: other_package/__init__.py"
    ]


def test_verify_release_wheel_rejects_unexpected_distribution_metadata(tmp_path):
    files = _valid_files()
    files["other_package-1.0.0.dist-info/METADATA"] = b"unexpected"
    wheel = _wheel(tmp_path / "frisket.whl", files)

    assert verify_wheel(str(wheel)) == [
        "unexpected package paths present: other_package-1.0.0.dist-info/METADATA"
    ]


def test_verify_release_wheel_accepts_public_staged_shape(tmp_path):
    files = _valid_files()
    files["frisket/integrations/hosted_error.py"] = b"public adapter"
    files["frisket_data-0.1.1a68.dist-info/METADATA"] = b"Name: frisket-data"
    wheel = _wheel(tmp_path / "frisket.whl", files)

    assert verify_wheel(str(wheel)) == []


def test_installed_probe_rejects_import_from_checkout(monkeypatch, tmp_path):
    source_package = tmp_path / "src" / "frisket" / "__init__.py"
    source_package.parent.mkdir(parents=True)
    source_package.touch()
    monkeypatch.setattr(
        probe_installed_release.frisket, "__file__", str(source_package)
    )

    with pytest.raises(
        probe_installed_release.ReleaseProbeError,
        match="imported from source checkout",
    ):
        probe_installed_release._assert_installed_import(tmp_path)
