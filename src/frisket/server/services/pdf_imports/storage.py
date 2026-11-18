"""Durable header, lock and expiry ownership for PDF import drafts."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

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
        cutoff = current_time.timestamp() - DRAFT_TTL_SECONDS
        for directory in self.root.glob(".adopt-pdf-*"):
            try:
                metadata = directory.lstat()
                if stat.S_ISDIR(metadata.st_mode) and metadata.st_mtime <= cutoff:
                    shutil.rmtree(directory, ignore_errors=True)
            except OSError:
                continue
        for directory in self.root.glob(".pdf-reader-*"):
            try:
                metadata = directory.lstat()
                if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mtime > cutoff:
                    continue
                with FileLock(str(directory / ".reader.lock"), timeout=0):
                    shutil.rmtree(directory, ignore_errors=True)
            except (OSError, Timeout):
                continue
        terminal = {"open", "committed", "cancelling", "cancelled", "error"}
        for path in self.root.glob("pdf-*/manifest.json"):
            directory = path.parent
            try:
                header = json.loads(path.read_text("utf-8"))
                expires = datetime.fromisoformat(
                    str(header["expires_at"]).replace("Z", "+00:00")
                )
                # A draft whose import is still unsealed stays until it settles.
                if (
                    expires > current_time
                    or header.get("state") not in terminal
                    or keeps_commit_inventory(header)
                ):
                    continue
                with self.lock(directory):
                    current = json.loads(path.read_text("utf-8"))
                    current_expiry = datetime.fromisoformat(
                        str(current["expires_at"]).replace("Z", "+00:00")
                    )
                    if (
                        current_expiry <= current_time
                        and current.get("state") in terminal
                        and not keeps_commit_inventory(current)
                    ):
                        shutil.rmtree(directory, ignore_errors=True)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue

    def drafts_with_commit_inventory(self) -> list[Path]:
        """Drafts whose commit still keeps a commit inventory, in name order."""
        found = []
        for path in sorted(self.root.glob("pdf-*/manifest.json")):
            try:
                header = json.loads(path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if keeps_commit_inventory(header):
                found.append(path.parent)
        return found

    def settle_commit_inventory(
        self, directory: Path, seal_import: Callable[[str, str], None]
    ) -> None:
        """Settle one draft whose commit inventory nothing publishes.

        Its published rows stay: the import is sealed before the draft is
        marked committed, so a failure leaves the draft to be settled again.
        A draft whose bytes never left it reopens for review; one whose files
        moved without publishing is cancelled.
        """
        with self.lock(directory):
            header = json.loads((directory / "manifest.json").read_text("utf-8"))
            if not keeps_commit_inventory(header):
                return
            commit = header["commit"]
            if commit.get("result") is not None:
                seal_import(
                    str(header["project_id"]), str(commit["result"]["import_ref"])
                )
                header["state"] = "committed"
            elif commit.get("promotion_target") is None:
                with PdfDraftInventory(directory / "inventory.db") as inventory:
                    inventory.reset_commit_projection()
                for suffix in ("", "-wal", "-shm"):
                    Path(f"{commit['commit_inventory_path']}{suffix}").unlink(
                        missing_ok=True
                    )
                header["state"] = "open"
                header["commit"] = None
            else:
                header["state"] = "cancelled"
            self.save(directory, header)

    def mark_unsettled(self, directory: Path) -> None:
        """Show a draft that could not be settled as needing attention.

        Its commit and files stay, so the next settlement tries it again.
        """
        with self.lock(directory):
            header = json.loads((directory / "manifest.json").read_text("utf-8"))
            if keeps_commit_inventory(header) and header["state"] != "error":
                header["state"] = "error"
                self.save(directory, header)


def draft_owner(header: Any) -> tuple[str, str]:
    """The project and storage identity a draft header names.

    Raises when the header does not name both, so its owner is unknown.
    """
    project_id, storage_identity = header["project_id"], header["storage_identity"]
    if not isinstance(project_id, str) or not isinstance(storage_identity, str):
        raise TypeError("draft header names no owner")
    return project_id, storage_identity


def unreadable_draft_count(workspace_root: Path) -> int:
    """Drafts whose header cannot be read well enough to name their owner."""
    count = 0
    for path in (Path(workspace_root) / ".pdf_import_drafts").glob(
        "pdf-*/manifest.json"
    ):
        try:
            draft_owner(json.loads(path.read_text("utf-8")))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError):
            count += 1
    return count


def keeps_commit_inventory(header: Mapping[str, Any]) -> bool:
    """Whether a committing draft keeps a commit inventory.

    Such a draft admitted uploads into an import after its first rows were
    published, and nothing publishes those uploads, so it is settled instead.
    """
    commit = header.get("commit")
    return (
        header.get("state") in {"committing", "error"}
        and isinstance(commit, dict)
        and "commit_inventory_path" in commit
        and commit.get("packet_publication") is not True
    )


__all__ = [
    "PdfDraftStore",
    "draft_owner",
    "unreadable_draft_count",
    "PdfImportDraftError",
    "SCHEMA_VERSION",
    "now",
]
