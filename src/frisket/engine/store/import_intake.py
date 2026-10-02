"""Owned-directory primitives for resumable import intake."""

from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from filelock import BaseFileLock, FileLock

from frisket.engine.store.import_inventory import ImportInventory


_REF = re.compile(r"import-[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class ImportIntakeHeader:
    project_id: str
    storage_identity: str
    envelope: Mapping[str, Any]
    cancel_requested: bool = False
    max_rows: int | None = None
    max_bytes: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "storage_identity": self.storage_identity,
            "envelope": dict(self.envelope),
            "cancel_requested": self.cancel_requested,
            "max_rows": self.max_rows,
            "max_bytes": self.max_bytes,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ImportIntakeHeader:
        return cls(
            project_id=str(value["project_id"]),
            storage_identity=str(value["storage_identity"]),
            envelope=dict(value["envelope"]),
            cancel_requested=bool(value.get("cancel_requested", False)),
            max_rows=value.get("max_rows"),
            max_bytes=value.get("max_bytes"),
        )


def validate_import_ref(ref: str) -> str:
    if not isinstance(ref, str) or _REF.fullmatch(ref) is None:
        raise ValueError("invalid import session reference")
    return ref


def import_intake_dir(project_path: str | Path, ref: str) -> Path:
    return Path(project_path) / ".imports" / validate_import_ref(ref)


def read_import_header(directory: Path) -> ImportIntakeHeader:
    value = json.loads((directory / "manifest.json").read_text())
    if not isinstance(value, dict):
        raise ValueError("import manifest must be an object")
    return ImportIntakeHeader.from_json(value)


def write_import_header(directory: Path, header: ImportIntakeHeader) -> None:
    payload = json.dumps(
        header.to_json(), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    temporary = directory / f".manifest-{secrets.token_hex(6)}"
    try:
        with temporary.open("w") as sink:
            sink.write(payload)
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(temporary, directory / "manifest.json")
        _fsync_directory(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def request_import_cancel(directory: Path) -> ImportIntakeHeader:
    header = replace(read_import_header(directory), cancel_requested=True)
    write_import_header(directory, header)
    return header


def import_admit_lock(directory: Path, *, timeout: float = -1) -> BaseFileLock:
    return FileLock(str(directory / ".admit.lock"), timeout=timeout)


def import_worker_lock(directory: Path, *, timeout: float = -1) -> BaseFileLock:
    return FileLock(str(directory / ".worker.lock"), timeout=timeout)


def append_inventory_batch(
    inventory: ImportInventory,
    batch_id: str,
    items: list[Mapping[str, Any]],
) -> tuple[int, bool]:
    """Atomically append one bounded batch and its retry marker.

    Returns ``(through_ordinal, added)``. The caller bounds ``items``; this
    function never represents the full session inventory.
    """

    if not isinstance(batch_id, str) or not batch_id or len(batch_id) > 128:
        raise ValueError("batch_id must be 1..128 characters")
    db = inventory._db
    payload_hash = _batch_payload_hash(items)
    db.execute(
        "CREATE TABLE IF NOT EXISTS inventory_batches ("
        "batch_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,"
        "through_ordinal INTEGER NOT NULL)"
    )
    db.execute("BEGIN IMMEDIATE")
    try:
        existing = db.execute(
            "SELECT payload_hash,through_ordinal FROM inventory_batches WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != payload_hash:
                raise ValueError("batch_id was already used for different files")
            db.commit()
            return int(existing["through_ordinal"]), False
        if inventory.sealed:
            raise RuntimeError("import inventory is sealed")
        count = total_bytes = through = 0
        for item in items:
            values = ImportInventory._validated(item)
            cursor = db.execute(
                "INSERT INTO inventory_items "
                "(logical_path,mime,sha256,size,kind,email_format) "
                "VALUES (?,?,?,?,?,?)",
                values,
            )
            through = int(cursor.lastrowid)
            count += 1
            total_bytes += values[3]
        if not count:
            raise ValueError("import batch must contain at least one item")
        db.execute(
            "UPDATE inventory_meta SET item_count=item_count+?,"
            "total_bytes=total_bytes+? WHERE singleton=1",
            (count, total_bytes),
        )
        db.execute(
            "INSERT INTO inventory_batches "
            "(batch_id,payload_hash,through_ordinal) VALUES (?,?,?)",
            (batch_id, payload_hash, through),
        )
        db.commit()
        return through, True
    except BaseException:
        db.rollback()
        raise


def inventory_batch_through(
    inventory: ImportInventory, batch_id: str, items: list[Mapping[str, Any]]
) -> int | None:
    """Return a matching bounded retry marker; reject batch-id payload changes."""

    if not isinstance(batch_id, str) or not batch_id or len(batch_id) > 128:
        raise ValueError("batch_id must be 1..128 characters")
    row = inventory._db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='inventory_batches'"
    ).fetchone()
    if row is None:
        return None
    existing = inventory._db.execute(
        "SELECT payload_hash,through_ordinal FROM inventory_batches WHERE batch_id=?",
        (batch_id,),
    ).fetchone()
    if existing is None:
        return None
    if existing["payload_hash"] != _batch_payload_hash(items):
        raise ValueError("batch_id was already used for different files")
    return int(existing["through_ordinal"])


def _batch_payload_hash(items: list[Mapping[str, Any]]) -> str:
    payload = json.dumps(
        [dict(item) for item in items],
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def inventory_item_from_staged(item: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only immutable facts; derived filename/path are not persisted."""

    logical_path = str(item["logical_path"])
    result = {
        "logical_path": logical_path,
        "mime": str(item["mime"]),
        "sha256": str(item["sha256"]),
        "size": int(item["size"]),
        "kind": str(item["kind"]),
    }
    email_format = item.get("email_format")
    if email_format is not None:
        result["email_format"] = str(email_format)
    # Force validation of the derived filename invariant at this boundary.
    if not PurePosixPath(logical_path).name:
        raise ValueError("inventory item requires a filename")
    return result


__all__ = [
    "ImportIntakeHeader",
    "append_inventory_batch",
    "import_admit_lock",
    "import_intake_dir",
    "import_worker_lock",
    "inventory_item_from_staged",
    "inventory_batch_through",
    "read_import_header",
    "request_import_cancel",
    "validate_import_ref",
    "write_import_header",
]
