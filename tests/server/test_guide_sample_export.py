from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from frisket.server.services.guide_samples import download, sample_metadata


def test_static_export_is_deterministic_and_matches_runtime(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts/docs/export_samples.py"
    outputs = [tmp_path / "first", tmp_path / "second"]
    for output in outputs:
        subprocess.run([sys.executable, str(script), str(output)], check=True)
    assert {p.name: p.read_bytes() for p in outputs[0].iterdir()} == {
        p.name: p.read_bytes() for p in outputs[1].iterdir()
    }
    catalog = json.loads((outputs[0] / "samples.json").read_text())
    for info in catalog["samples"]:
        sample_id, revision = info["id"], info["revision"]
        assert {k: v for k, v in info.items() if k != "downloadUrl"} == sample_metadata(
            sample_id, revision
        )
        for item in info["files"]:
            assert item["source_url"] and item["license"] and item["author"]
            if item["media_type"] == "application/pdf":
                assert item["license"] == "Apache-2.0"
                assert "synthetic" in item["author"]
            elif item["media_type"] == "text/csv":
                assert item["license"] == "Apache-2.0"
                assert "companion" in item["author"]
        filename = f"{sample_id}-r{revision}.zip"
        assert info["downloadUrl"] == f"/assets/docs/samples/{filename}"
        assert (outputs[0] / filename).read_bytes() == download(sample_id, revision)
    audio = catalog["samples"][1]["files"][0]
    assert audio["source_url"] and audio["license"] == "CC0-1.0" and audio["author"]
    assert (
        subprocess.run(
            [sys.executable, str(script), str(outputs[0])], capture_output=True
        ).returncode
        != 0
    )
