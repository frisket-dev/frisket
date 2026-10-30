"""Public bundle composition and failure behavior."""

import base64
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/docs/build_bundle.py"
spec = importlib.util.spec_from_file_location("docs_bundle_build", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

SHA = "a" * 40
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def inputs(tmp_path):
    plans = tmp_path / "plans"
    captures = tmp_path / "capture"
    plans.mkdir()
    captures.mkdir()
    recipe = {
        "id": "guide",
        "sample": {"id": "filing", "revision": 1},
        "steps": [
            {
                "id": "extract",
                "actionId": "map.extract",
                "settings": [{"id": "prompt"}],
                "captureIds": ["setup"],
            }
        ],
    }
    write_json(plans / "guide.json", recipe)
    catalog = tmp_path / "catalog.json"
    write_json(
        catalog,
        {
            "actions": [
                {
                    "id": "map.extract",
                    "label": "Extract fields",
                    "path": ["Analyze", "EXTRACT"],
                    "params": [],
                }
            ]
        },
    )
    samples = tmp_path / "samples.json"
    write_json(samples, {"samples": [{"id": "filing", "revision": 1}]})
    record = {
        "id": "setup",
        "image": "setup.png",
        "width": 1,
        "height": 1,
        "scale": 2,
        "annotations": [
            {
                "id": "prompt",
                "number": 1,
                "label": "Instruction",
                "value": "Find it",
                "rect": {"x": 0, "y": 0, "width": 1, "height": 1},
            }
        ],
    }
    write_json(
        captures / "bundle.json",
        {
            "version": 1,
            "appRevision": SHA,
            "sample": recipe["sample"],
            "captures": [record],
        },
    )
    (captures / "setup.png").write_bytes(PNG)
    return {
        "plans": plans,
        "catalog": catalog,
        "samples": samples,
        "captures": [captures],
        "output": tmp_path / "output",
        "app_revision": SHA,
    }


def test_composes_projection_and_publishes_image(inputs):
    module.build_bundle(**inputs)
    output = inputs["output"]
    bundle = json.loads((output / "guideBundle.json").read_text())
    assert bundle["actions"][0]["uiPath"] == ["Analyze", "EXTRACT", "Extract fields"]
    assert bundle["actions"][0]["name"] == "Extract fields"
    assert bundle["captures"][0]["image"] == f"/assets/docs/{SHA[:12]}/setup.png"
    assert (output / bundle["captures"][0]["image"].lstrip("/")).read_bytes() == PNG


@pytest.mark.parametrize(
    "failure", ["sha", "missing", "sample", "geometry", "dimensions", "duplicate"]
)
def test_invalid_evidence_never_publishes(inputs, failure):
    directory = inputs["captures"][0]
    bundle_path = directory / "bundle.json"
    bundle = json.loads(bundle_path.read_text())
    if failure == "sha":
        inputs["app_revision"] = "../bad"
    elif failure == "missing":
        (directory / "setup.png").unlink()
    elif failure == "sample":
        bundle["sample"]["revision"] = 2
    elif failure == "geometry":
        bundle["captures"][0]["annotations"][0]["rect"]["x"] = 0.5
    elif failure == "dimensions":
        bundle["captures"][0]["width"] = 2
    else:
        bundle["captures"].append(bundle["captures"][0])
    write_json(bundle_path, bundle)
    with pytest.raises(ValueError):
        module.build_bundle(**inputs)
    assert not inputs["output"].exists()
    assert not list(inputs["output"].parent.glob(".output-*"))


def test_existing_output_is_preserved(inputs):
    inputs["output"].mkdir()
    marker = inputs["output"] / "previous"
    marker.write_text("preserved")
    with pytest.raises(ValueError, match="already exists"):
        module.build_bundle(**inputs)
    assert marker.read_text() == "preserved"


def test_failed_copy_cleans_staging_and_preserves_absent_output(inputs, monkeypatch):
    def fail_copy(*_args):
        raise OSError("copy failed")

    monkeypatch.setattr(module.shutil, "copyfile", fail_copy)
    with pytest.raises(OSError, match="copy failed"):
        module.build_bundle(**inputs)
    assert not inputs["output"].exists()
    assert not list(inputs["output"].parent.glob(".output-*"))
