"""HTTP contract chain parity: the generated TS contract file cannot go stale.

``scripts/ci/gen_http_contracts.py``
--check`` existed but was invoked by NOTHING — a backend contract change could
ship with a stale ``web/src/generated/openHttpContracts.ts`` and no signal.
Same pattern as tests/server/test_ocr_engine_catalog_sync.py.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG
from scripts.ci import export_web_openapi as exporter

ROOT = Path(__file__).resolve().parents[2]


def test_generated_http_contracts_are_in_sync(tmp_path: Path) -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/ci/gen_http_contracts.py"), "--check"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_generated_action_output_keeps_its_public_fields() -> None:
    # rule19: generated artifact field-parity contract
    generated = (ROOT / "web/src/generated/openHttpContracts.ts").read_text(
        encoding="utf-8"
    )
    block = generated.split("type HttpActionResult_ActionOutputPayload = ({", 1)[
        1
    ].split("\n\nexport type HttpActionResult", 1)[0]
    for field in ("kind", "name", "sheet_id", "column_id", "row_ids", "ref"):
        assert f'"{field}"' in block


def test_real_composition_operation_ids_match_declared_browser_surface(
    tmp_path: Path,
) -> None:
    import frisket.team.app as team_app

    document = exporter.export_real_compositions(tmp_path / "real-compositions")
    actual = {
        str(operation["operationId"])
        for path_item in document["paths"].values()
        for operation in path_item.values()
    }
    declared = {
        exporter.CANONICAL_OPERATION_IDS.get(
            (entry.route_owner, entry.route_name, entry.method.upper()),
            entry.id,
        )
        for entry in (
            *BASE_ENDPOINT_CATALOG,
            *team_app._TEAM_LOCAL_MODEL_DECLARATIONS,
            *team_app._TEAM_BROWSER_AUTH_DECLARATIONS,
        )
        if entry.browser_client
    }
    assert actual == declared
