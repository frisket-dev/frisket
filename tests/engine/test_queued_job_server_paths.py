"""A queued job opens request-supplied server paths only for the local edition."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from frisket.actions.system import typed_action_for_request
from frisket.actions.types import TableError
from frisket.authoring.action_metadata import PRODUCT_EDITION_SNAPSHOT_KEY
from frisket.engine.executor.action_inventory import ExecutorDeps
from frisket.engine.executor.action_jobs import (
    reserve_typed_action_job,
    run_action_run_job,
)
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.executor.import_sources import open_local_file_reader
from frisket.engine.store import Project

SECRET = "server-only-bytes"


def _queued_update(project: Project, source: Path, edition: str | None):
    seeded = run_action_spec(
        project,
        {
            "action_id": "import.rows",
            "scope": {"kind": "project"},
            "sheet_name": "people",
            "params": {
                "columns": [
                    {"name": "name", "type": "text"},
                    {"name": "note", "type": "text"},
                ],
                "rows": [{"name": "Ada", "note": "seed"}],
            },
            "idempotency_key": "seed",
        },
        project_id="p",
    )
    assert seeded.status == "completed", seeded.errors
    bound = typed_action_for_request(
        {
            "action_id": "import.update_csv",
            "scope": {"kind": "sheet_rows", "sheet_id": 1},
            "params": {
                "sources": [{"path": str(source)}],
                "columns": [
                    {"name": "name", "type": "text"},
                    {"name": "note", "type": "text"},
                ],
                "key_columns": ["name"],
            },
            "idempotency_key": f"update-{edition}",
        }
    )
    snapshot = None if edition is None else {PRODUCT_EDITION_SNAPSHOT_KEY: edition}
    envelope = reserve_typed_action_job(
        project, "p", bound, edition_run_context=snapshot
    )
    return run_action_run_job(project, {"action_job": envelope.to_json(), "job_id": 1})


@pytest.mark.parametrize("edition", ["team", "cloud"])
def test_multi_user_job_cannot_open_a_server_path(tmp_path: Path, edition) -> None:
    source = tmp_path / "secret.csv"
    source.write_text(f"name,note\nAda,{SECRET}\n")
    with closing(Project.create(tmp_path / "project")) as project:
        result = _queued_update(project, source, edition)
        assert result.status == "failed", result
        assert "not available on this deployment" in str(result.errors)
        staged = [
            path
            for path in project.path.rglob("*")
            if path.is_file() and SECRET.encode() in path.read_bytes()
        ]
        assert staged == []


def test_local_job_still_reads_the_operator_path(tmp_path: Path) -> None:
    source = tmp_path / "secret.csv"
    source.write_text(f"name,note\nAda,{SECRET}\n")
    with closing(Project.create(tmp_path / "project")) as project:
        result = _queued_update(project, source, None)
        assert "not available on this deployment" not in str(result.errors)


def test_reader_without_admitted_sources_or_grant_refuses(tmp_path: Path) -> None:
    source = tmp_path / "plain.txt"
    source.write_text("bytes")
    with open_local_file_reader(ExecutorDeps()) as reader:
        with pytest.raises(TableError, match="not available on this deployment"):
            reader.read_bytes(str(source))
    with open_local_file_reader(ExecutorDeps(local_file_sources={})) as reader:
        with pytest.raises(TableError):
            reader.read_bytes(str(source))
    with open_local_file_reader(ExecutorDeps(operator_local_files=True)) as reader:
        assert reader.read_bytes(str(source)) == b"bytes"
