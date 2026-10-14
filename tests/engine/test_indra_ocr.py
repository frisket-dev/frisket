from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from frisket.ai.models.artifact_manifest import lookup
from frisket.engine._workers.indra_ocr_model import (
    MODEL_DOWNLOAD_SIZE,
    MODEL_FILES,
    MODEL_LICENSE,
    MODEL_LICENSE_URL,
    MODEL_REF,
    MODEL_REPO,
    MODEL_REVISION,
)
from frisket.engine._workers.indra_ocr_worker import _original_box, _page_result
from frisket.engine.sandbox.shim import SandboxResult
from frisket.ops import ocr_engines_local


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        (0, [10.0, 20.0, 30.0, 40.0]),
        (90, [20.0, 70.0, 40.0, 90.0]),
        (180, [170.0, 60.0, 190.0, 80.0]),
        (270, [160.0, 10.0, 180.0, 30.0]),
    ],
)
def test_indra_line_boxes_map_from_upright_page_to_input(rotation, expected):
    assert (
        _original_box([10, 20, 30, 40], rotation=rotation, width=200, height=100)
        == expected
    )


def test_indra_page_projection_preserves_reading_order_score_and_geometry():
    page = SimpleNamespace(
        source="ocr",
        rotation=90,
        skew=1.7,
        lines=[
            SimpleNamespace(text=" First line ", box=[10, 20, 30, 40], conf=0.98765),
            SimpleNamespace(text="Second line", box=[35, 25, 70, 50], conf=0.75),
        ],
    )

    result = _page_result(page, width=200, height=100)

    assert result["text"] == "First line\nSecond line"
    assert result["blocks"] == [
        {
            "text": "First line",
            "bbox": [[20.0, 70.0], [40.0, 70.0], [40.0, 90.0], [20.0, 90.0]],
            "score": 0.9877,
        },
        {
            "text": "Second line",
            "bbox": [[25.0, 30.0], [50.0, 30.0], [50.0, 65.0], [25.0, 65.0]],
            "score": 0.75,
        },
    ]
    with pytest.raises(RuntimeError, match="could not read"):
        _page_result(SimpleNamespace(source="error"), width=200, height=100)


def test_indra_model_manifest_is_revision_pinned_with_exact_files():
    artifact = lookup(MODEL_REF)
    assert artifact is not None
    assert artifact.integrity == "hf_revision_pinned"
    assert artifact.license_url == MODEL_LICENSE_URL
    assert artifact.license == "LicenseRef-Datalab-Indra-OpenRAIL-M-Modified"
    assert MODEL_LICENSE_URL == (
        "https://huggingface.co/datalab-to/indra-ocr/blob/"
        "d7c839c892bfe4b8993373db64c62f154d05d70b/LICENSE"
    )
    assert MODEL_LICENSE == artifact.license
    assert artifact.approx_size_bytes == MODEL_DOWNLOAD_SIZE
    assert artifact.hf_snapshot is not None
    assert artifact.hf_snapshot.repo_id == MODEL_REPO
    assert artifact.hf_snapshot.revision == MODEL_REVISION
    assert artifact.hf_snapshot.files == MODEL_FILES


def test_indra_local_adapter_uses_private_runtime_offline_and_validates_pages(
    tmp_path, monkeypatch
):
    from frisket.engine._workers import indra_ocr_artifacts
    from frisket.runtime import indra_ocr_install

    repo_cache = tmp_path / "models--datalab-to--indra-ocr"
    model_dir = repo_cache / "snapshots" / MODEL_REVISION
    model_dir.mkdir(parents=True)
    page = tmp_path / "page.png"
    page.touch()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    runtime = tmp_path / "private" / "bin" / "python"
    runtime.parent.mkdir(parents=True)
    runtime.touch(mode=0o755)
    monkeypatch.setattr(indra_ocr_artifacts, "indra_ocr_runtime_present", lambda: True)
    monkeypatch.setattr(indra_ocr_artifacts, "cached_indra_ocr_path", lambda: model_dir)
    monkeypatch.setattr(indra_ocr_install, "runtime_python", lambda: runtime)
    captured = {}

    async def run_sandboxed(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        payload = json.loads(kwargs["stdin_data"])
        Path(payload["out"]).write_text(
            json.dumps({"pages": [{"text": "local", "blocks": []}]}),
            encoding="utf-8",
        )
        captured["payload"] = payload
        return SandboxResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ocr_engines_local, "run_sandboxed", run_sandboxed)

    result = asyncio.run(ocr_engines_local.ocr_indra([page], scratch))

    assert result == [{"text": "local", "blocks": []}]
    assert captured["argv"][0] == str(runtime)
    assert captured["argv"][3] == "indra-ocr"
    assert captured["payload"]["model_dir"] == str(model_dir)
    assert captured["policy"].allow_network is False
    assert captured["policy"].trusted_python_netwall is True
    assert str(page) in captured["policy"].confine.read
    assert str(repo_cache) in captured["policy"].confine.read
    assert captured["policy"].confine.write == (str(scratch),)
