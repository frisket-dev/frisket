"""Private durable storage for table-import drafts."""

from __future__ import annotations

import json
import os
import secrets
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from filelock import FileLock

from frisket.server.route_errors import RouteError
from frisket.server.services.import_bulk_sources import fsync_directory


DRAFT_TTL = timedelta(days=1)


class TableImportDraftError(RouteError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class TableImportDraftStore:
    def __init__(self, workspace: Any) -> None:
        self.workspace = workspace

    def create_directory(self, project_id: str) -> tuple[str, Path]:
        project = self.workspace.get(project_id)
        root = Path(project.path) / ".imports"
        root.mkdir(parents=True, exist_ok=True)
        for _ in range(100):
            draft_id = f"import-{secrets.token_hex(16)}"
            directory = root / draft_id
            try:
                directory.mkdir(mode=0o700)
                return draft_id, directory
            except FileExistsError:
                continue
        raise RuntimeError("could not allocate table import draft")

    def open(self, project_id: str, draft_id: str) -> tuple[Path, dict[str, Any]]:
        if not draft_id.startswith("import-") or "/" in draft_id or "\\" in draft_id:
            raise TableImportDraftError(404, "table import draft not found")
        project = self.workspace.get(project_id)
        directory = Path(project.path) / ".imports" / draft_id
        if not directory.is_dir():
            raise TableImportDraftError(404, "table import draft not found")
        header = self.read_json(directory / "table-draft.json")
        if (
            header.get("schema_version") != "frisket.table_import.v1"
            or header.get("project_id") != project_id
            or header.get("storage_identity") != project.storage_identity
        ):
            raise TableImportDraftError(404, "table import draft not found")
        return directory, header

    @staticmethod
    def lock(directory: Path) -> FileLock:
        return FileLock(str(directory / ".table-draft.lock"), timeout=10)

    @staticmethod
    def commit_lock(directory: Path) -> FileLock:
        return FileLock(str(directory / ".table-commit.lock"), timeout=60)

    @staticmethod
    def read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TableImportDraftError(404, "table import draft not found") from exc
        if not isinstance(value, dict):
            raise TableImportDraftError(404, "table import draft not found")
        return value

    @staticmethod
    def write_json(path: Path, value: Mapping[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}")
        try:
            with temporary.open("x", encoding="utf-8") as sink:
                json.dump(value, sink, sort_keys=True, separators=(",", ":"))
                sink.flush()
                os.fsync(sink.fileno())
            os.replace(temporary, path)
            fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def save_header(self, directory: Path, header: dict[str, Any]) -> None:
        header["revision"] = int(header["revision"]) + 1
        header["updated_at"] = now()
        self.write_json(directory / "table-draft.json", header)

    @staticmethod
    def scan(directory: Path) -> dict[str, Any] | None:
        path = directory / "table-preview.json"
        if not path.exists():
            return None
        return TableImportDraftStore.read_json(path)

    def discard(self, directory: Path) -> None:
        shutil.rmtree(directory)

    def purge_expired(self, project_id: str) -> None:
        project = self.workspace.get(project_id)
        root = Path(project.path) / ".imports"
        if not root.is_dir():
            return
        cutoff = datetime.now(UTC) - DRAFT_TTL
        for path in root.glob("import-*/table-draft.json"):
            try:
                header = self.read_json(path)
                if header.get("state") in {"committing", "committed"}:
                    continue
                updated = datetime.fromisoformat(
                    str(header["updated_at"]).replace("Z", "+00:00")
                )
                if updated > cutoff:
                    continue
                directory = path.parent
                with self.lock(directory):
                    current = self.read_json(path)
                    current_updated = datetime.fromisoformat(
                        str(current["updated_at"]).replace("Z", "+00:00")
                    )
                    if (
                        current.get("state") not in {"committing", "committed"}
                        and current_updated <= cutoff
                    ):
                        shutil.rmtree(directory)
            except (KeyError, OSError, ValueError, TableImportDraftError):
                continue


__all__ = ["TableImportDraftError", "TableImportDraftStore", "now"]
