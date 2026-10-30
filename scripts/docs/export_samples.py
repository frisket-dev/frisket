#!/usr/bin/env python3
"""Export the app's first-party sample files for public static documentation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from frisket.server.services.guide_samples import download, sample_metadata  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new output directory")
    output = parser.parse_args().output
    output.mkdir(parents=True, exist_ok=False)
    samples = []
    for sample_id in ("court-filings", "council-recordings"):
        info = sample_metadata(sample_id)
        filename = f"{sample_id}-r{info['revision']}.zip"
        (output / filename).write_bytes(download(sample_id, info["revision"]))
        samples.append({**info, "downloadUrl": f"/assets/docs/samples/{filename}"})
    (output / "samples.json").write_text(
        json.dumps({"samples": samples}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
