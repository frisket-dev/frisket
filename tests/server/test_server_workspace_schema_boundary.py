from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from frisket.server import schemas


ROOT = Path(__file__).resolve().parents[2]


def test_schema_defaults_and_validation_contracts_are_preserved() -> None:
    assert schemas.ReadRangeBody(sheet_id=1).model_dump() == {
        "sheet_id": 1,
        "offset": 0,
        "limit": 50,
        "columns": None,
    }
    # Public onboarding request bodies belong to their generated HTTP-contract
    # module, not the server-local schema catch-all.
    with pytest.raises(AttributeError):
        schemas.ImportPasteDraftBody
    with pytest.raises(AttributeError):
        schemas.ImportUrlsBody


def test_server_app_imports_in_fresh_process() -> None:
    subprocess.run(
        [sys.executable, "-c", "import frisket.server.app"],
        cwd=ROOT,
        check=True,
    )
