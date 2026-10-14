from __future__ import annotations

import tomllib
from pathlib import Path


def test_indra_runtime_profile_is_exact_pinned_and_packaged() -> None:
    root = Path(__file__).resolve().parents[2]
    sidecar_path = root / "sidecar" / "pyproject.toml"
    sidecar = tomllib.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["project"]["optional-dependencies"]["ocr-indra"] == [
        "indra-ocr==0.3.1"
    ]
    assert sidecar["tool"]["uv"]["exclude-newer-package"] == {"indra-ocr": "2026-10-13"}

    constraints = (root / "sidecar" / "indra-ocr-constraints.txt").read_text(
        encoding="utf-8"
    )
    assert "#    uv export --extra ocr-indra" in constraints
    assert "indra-ocr==0.3.1" in constraints
    assert "onnxruntime==1.26.0" in constraints
    assert "opencv-python-headless==4.11.0.86" in constraints
    assert "huggingface-hub==1.31.0" in constraints

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["tool"]["uv"]["exclude-newer-package"]["indra-ocr"] == ("2026-10-13")
    build = project["tool"]["hatch"]["build"]["targets"]
    assert "sidecar/indra-ocr-constraints.txt" in build["sdist"]["only-include"]
    assert (
        build["wheel"]["force-include"]["sidecar/indra-ocr-constraints.txt"]
        == "frisket/data/model-server/indra-ocr-constraints.txt"
    )


def test_indra_wheel_hash_and_resolved_runtime_are_locked() -> None:
    root = Path(__file__).resolve().parents[2]
    lock = tomllib.loads((root / "sidecar" / "uv.lock").read_text(encoding="utf-8"))
    package = next(row for row in lock["package"] if row["name"] == "indra-ocr")
    assert package["version"] == "0.3.1"
    assert package["wheels"] == [
        {
            "url": "https://files.pythonhosted.org/packages/fe/8c/0b22e4d17f8c7a84fa6a26fe6b97fcbd097b1186715a544265d49a1aafb0/indra_ocr-0.3.1-py3-none-any.whl",
            "hash": "sha256:9a99d2a18cf288c336c065946204be45f8cffb3bc37e96357b73939251442ed2",
            "size": 76530,
            "upload-time": "2026-10-13T15:38:10.337Z",
        }
    ]
