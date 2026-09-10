from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest

from executor_harness import case_env
from frisket.engine.executor import action_lifecycle
from frisket.engine.executor import embedding_export
from tests.engine.test_embedding_index_export_executor import CASES, _export_action
from tests.engine.test_export_delivery_failure import (
    _receipt_count,
    _wrap_project_db_commit,
)


def _files(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in directory.iterdir()}


def _existing_artifacts(env) -> dict[str, bytes]:
    for fmt in ("jsonl", "parquet"):
        path = env.seeded["out"] / f"{env.seeded['index_id']}.embeddings.{fmt}"
        path.write_bytes(f"previous {fmt} bytes\n".encode())
    return _files(env.seeded["out"])


def _multi_action(env):
    return _export_action(
        env.seeded["index_id"], env.seeded["out"], formats=["jsonl", "parquet"]
    )


def test_export_refuses_to_replace_directory_at_artifact_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        destination = env.seeded["out"] / f"{env.seeded['index_id']}.embeddings.jsonl"
        destination.mkdir()
        sentinel = destination / "keep.txt"
        sentinel.write_bytes(b"existing directory contents")
        before_receipts = _receipt_count(env.project)

        result = env.run_primary()

        assert result.status == "failed"
        assert result.errors[0].code == "invalid_export_destination"
        assert sentinel.read_bytes() == b"existing directory contents"
        assert list(env.seeded["out"].iterdir()) == [destination]
        assert _receipt_count(env.project) == before_receipts


def test_later_format_delivery_failure_restores_every_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        before_files = _existing_artifacts(env)
        before_receipts = _receipt_count(env.project)
        replace = Path.replace
        delivery_failed = False

        def fail_parquet_delivery(path, target):
            nonlocal delivery_failed
            if path.suffix == ".tmp" and Path(target).suffix == ".parquet":
                delivery_failed = True
                raise OSError("injected second-format delivery failure")
            return replace(path, target)

        monkeypatch.setattr(Path, "replace", fail_parquet_delivery)
        result = env.run(_multi_action(env))

        assert delivery_failed
        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert _files(env.seeded["out"]) == before_files
        assert _receipt_count(env.project) == before_receipts


@pytest.mark.parametrize("failure", ["receipt_insert", "commit"])
def test_database_failure_preserves_previous_files_and_no_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        before_files = _existing_artifacts(env)
        before_receipts = _receipt_count(env.project)
        if failure == "receipt_insert":
            env.project.db.execute(
                "CREATE TEMP TRIGGER reject_export_receipt BEFORE INSERT ON receipts "
                "WHEN NEW.action_kind = 'embedding.index_export' "
                "BEGIN SELECT RAISE(ABORT, 'injected receipt insertion failure'); END"
            )
            env.project.db.commit()
        else:
            connection = _wrap_project_db_commit(monkeypatch, env.project)
            connection.fail_commit = True

        result = env.run(_multi_action(env))

        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert _files(env.seeded["out"]) == before_files
        assert _receipt_count(env.project) == before_receipts


def test_encoding_failure_cleans_prior_and_partial_current_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        before_files = _existing_artifacts(env)
        before_receipts = _receipt_count(env.project)
        encoding_failed = False

        def fail_encoding(table, where, *args, **kwargs):
            nonlocal encoding_failed
            encoding_failed = True
            Path(where).write_bytes(b"partial parquet")
            raise OSError("injected encoder failure after partial write")

        monkeypatch.setattr(pq, "write_table", fail_encoding)
        result = env.run(_multi_action(env))

        assert encoding_failed
        assert result.status == "failed"
        assert result.errors[0].code == "project_write_failed"
        assert _files(env.seeded["out"]) == before_files
        assert _receipt_count(env.project) == before_receipts


def test_idempotency_race_cleanup_preserves_winners_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        action = _multi_action(env)
        winner = env.run(action)
        assert winner.status == "completed", winner.errors
        before_files = _files(env.seeded["out"])
        before_receipts = _receipt_count(env.project)
        lookup = action_lifecycle._receipt_for_idempotency
        lookups = 0

        def reveal_winner_on_recheck(project, key):
            nonlocal lookups
            if key == action["idempotency_key"]:
                lookups += 1
                if lookups == 1:
                    return None
            return lookup(project, key)

        monkeypatch.setattr(
            action_lifecycle, "_receipt_for_idempotency", reveal_winner_on_recheck
        )
        loser = env.run(action)

        assert lookups >= 2
        assert loser.status == "completed", loser.errors
        assert loser.receipt_id == winner.receipt_id
        assert _files(env.seeded["out"]) == before_files
        assert _receipt_count(env.project) == before_receipts


def test_postcommit_cleanup_failure_does_not_revert_successful_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        previous_files = _existing_artifacts(env)
        before_receipts = _receipt_count(env.project)
        cleanup_failed = False

        def fail_cleanup(resolved):
            nonlocal cleanup_failed
            cleanup_failed = True
            raise OSError("injected postcommit cleanup failure")

        monkeypatch.setattr(embedding_export, "_finalize_index_export", fail_cleanup)
        result = env.run(_multi_action(env))

        assert cleanup_failed
        assert "action_post_commit_cleanup_failed" in caplog.text
        assert result.status == "completed", result.errors
        assert _receipt_count(env.project) == before_receipts + 1
        assert result.receipt_id is not None
        for output in result.outputs:
            path = Path(output.ref["path"])
            assert path.read_bytes() != previous_files[path.name]
        replay = env.run(_multi_action(env))
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == result.receipt_id
