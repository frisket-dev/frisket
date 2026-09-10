from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_direct_root_validation_accepts_registry_backed_geo_point_in_fresh_process(
    tmp_path: Path,
) -> None:
    ndjson_path = tmp_path / "places.ndjson"
    ndjson_path.write_text(
        json.dumps(
            {
                "name": "Civic Hall",
                "location": {"lat": 40.7128, "lon": -74.006},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    code = r"""
import json
import os

from frisket.actions.system import validate_root_action


def action(column_type):
    return {
        "action_id": "import.ndjson",
        "scope": {"kind": "project"},
        "sheet_name": "Places",
        "params": {
            "source": {
                "kind": "file",
                "path": os.environ["NDJSON_PATH"],
                "label": "places.ndjson",
            },
            "encoding": "utf-8",
            "columns": [
                {"name": "name", "type": "text"},
                {"name": "location", "type": column_type},
            ],
        },
        "idempotency_key": f"direct-validation-{column_type}",
    }


print(
    json.dumps(
        {
            "geo_point": validate_root_action(action("geo_point")).model_dump(
                mode="json"
            ),
            "unknown": validate_root_action(action("not_registered")).model_dump(
                mode="json"
            ),
        },
        sort_keys=True,
    )
)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={
            **os.environ,
            "NDJSON_PATH": str(ndjson_path),
            "PYTHONPATH": str(ROOT / "src"),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert payload["geo_point"]["ok"] is True
    assert payload["geo_point"]["params"]["columns"][1]["type"] == "geo_point"
    assert payload["unknown"]["ok"] is False
    assert payload["unknown"]["error"]["code"] == "invalid_action_request"
    assert "invalid_column_type" in payload["unknown"]["error"]["message"]
