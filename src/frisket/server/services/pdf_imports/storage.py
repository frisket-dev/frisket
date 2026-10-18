"""Durable header, lock and expiry ownership for PDF import drafts."""

from __future__ import annotations

import json
import os
import secrets
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock

from frisket.server.route_errors import RouteError
from frisket.server.services.import_bulk_sources import fsync_directory

from .inventory import PdfDraftInventory


SCHEMA_VERSION = "frisket.pdf_import.v1"
DRAFT_TTL_SECONDS = 86_400


class PdfImportDraftError(RouteError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class PdfDraftStore:
    def __init__(self, workspace: Any) -> None:
        self.workspace = workspace
        self.root = Path(workspace.root) / ".pdf_import_drafts"
        self.root.mkdir(parents=True, exist_ok=True)

    def open(self, project_id: str, draft_id: str) -> tuple[Path, dict[str, Any]]:
        if not draft_id.startswith("pdf-") or "/" in draft_id or "\\" in draft_id:
            raise PdfImportDraftError(404, "PDF import draft not found")
        directory = self.root / draft_id
        if not directory.is_dir():
            raise PdfImportDraftError(404, "PDF import draft not found")
        return directory, self.header(project_id, directory)

    def header(self, project_id: str, directory: Path) -> dict[str, Any]:
        try:
            header = json.loads((directory / "manifest.json").read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PdfImportDraftError(404, "PDF import draft not found") from exc
        project = self.workspace.get(project_id)
        if (
            header.get("schema_version") != SCHEMA_VERSION
            or header.get("project_id") != project_id
            or header.get("storage_identity") != project.storage_identity
        ):
            raise PdfImportDraftError(404, "PDF import draft not found")
        return header

    @staticmethod
    def lock(directory: Path) -> FileLock:
        return FileLock(str(directory / ".admit.lock"))

    def save(self, directory: Path, header: dict[str, Any]) -> None:
        header["revision"] += 1
        header["updated_at"] = now()
        header["expires_at"] = self.expiry()
        self.write_header(directory, header)

    @staticmethod
    def write_header(directory: Path, header: Mapping[str, Any]) -> None:
        path = directory / "manifest.json"
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}")
        try:
            with temporary.open("x", encoding="utf-8") as sink:
                json.dump(header, sink, sort_keys=True, separators=(",", ":"))
                sink.flush()
                os.fsync(sink.fileno())
            os.replace(temporary, path)
            fsync_directory(directory)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def stats(directory: Path) -> dict[str, int]:
        with PdfDraftInventory(directory / "inventory.db") as inventory:
            return inventory.stats()

    @staticmethod
    def expiry() -> str:
        return (
            datetime.fromtimestamp(
                datetime.now(UTC).timestamp() + DRAFT_TTL_SECONDS, UTC
            )
            .isoformat()
            .replace("+00:00", "Z")
        )

    def purge_expired(self) -> None:
        current_time = datetime.now(UTC)
        terminal = {"open", "cancelled", "error"}
        for path in self.root.glob("pdf-*/manifest.json"):
            directory = path.parent
            try:
                header = json.loads(path.read_text("utf-8"))
                expires = datetime.fromisoformat(
                    str(header["expires_at"]).replace("Z", "+00:00")
                )
                if expires > current_time or header.get("state") not in terminal:
                    continue
                with self.lock(directory):
                    current = json.loads(path.read_text("utf-8"))
                    current_expiry = datetime.fromisoformat(
                        str(current["expires_at"]).replace("Z", "+00:00")
                    )
                    if (
                        current_expiry <= current_time
                        and current.get("state") in terminal
                    ):
                        shutil.rmtree(directory, ignore_errors=True)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue


__all__ = ["PdfDraftStore", "PdfImportDraftError", "SCHEMA_VERSION", "now"]
