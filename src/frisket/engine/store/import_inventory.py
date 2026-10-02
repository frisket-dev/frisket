"""Paged occurrence inventory for resumable imports.

``ImportInventory(path)`` owns one SQLite file inside a caller-owned plan
directory. ``append(items)`` consumes existing-style admitted item mappings
incrementally and assigns immutable, monotonic occurrence ordinals. ``page``
returns those mappings with redundant ``filename`` and content-addressed
``path`` fields derived on read. ``totals`` is a persisted O(1) summary, and
``seal`` permanently refuses later appends. Paging remains available before
and after sealing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any


class ImportInventory:
    """Store one ordered import occurrence inventory in a caller-chosen file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS inventory_meta (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                item_count INTEGER NOT NULL,
                total_bytes INTEGER NOT NULL,
                sealed INTEGER NOT NULL CHECK (sealed IN (0, 1))
            );
            INSERT OR IGNORE INTO inventory_meta
                (singleton, item_count, total_bytes, sealed)
                VALUES (1, 0, 0, 0);
            CREATE TABLE IF NOT EXISTS inventory_items (
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                logical_path TEXT NOT NULL,
                mime TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL CHECK (size >= 0),
                kind TEXT NOT NULL,
                email_format TEXT
            );
            CREATE TRIGGER IF NOT EXISTS inventory_items_no_update
            BEFORE UPDATE ON inventory_items
            BEGIN
                SELECT RAISE(ABORT, 'inventory records are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS inventory_items_no_delete
            BEFORE DELETE ON inventory_items
            BEGIN
                SELECT RAISE(ABORT, 'inventory records are immutable');
            END;
            """
        )

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> ImportInventory:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def sealed(self) -> bool:
        row = self._db.execute(
            "SELECT sealed FROM inventory_meta WHERE singleton = 1"
        ).fetchone()
        return bool(row["sealed"])

    def totals(self) -> dict[str, int]:
        """Return persisted occurrence count and admitted byte total."""

        row = self._db.execute(
            "SELECT item_count, total_bytes FROM inventory_meta WHERE singleton = 1"
        ).fetchone()
        return {"count": int(row["item_count"]), "bytes": int(row["total_bytes"])}

    def append(self, items: Iterable[Mapping[str, Any]]) -> tuple[int, int]:
        """Append an iterable atomically; return first and last new ordinals.

        An empty iterable returns ``(0, 0)``. Input is consumed one item at a
        time and is never copied into a corpus-sized intermediate collection.
        """

        first = last = 0
        count = total_bytes = 0
        self._db.execute("BEGIN IMMEDIATE")
        try:
            if self.sealed:
                raise RuntimeError("import inventory is sealed")
            for item in items:
                values = self._validated(item)
                cursor = self._db.execute(
                    """
                    INSERT INTO inventory_items
                        (logical_path, mime, sha256, size, kind, email_format)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                ordinal = int(cursor.lastrowid)
                if not first:
                    first = ordinal
                last = ordinal
                count += 1
                total_bytes += values[3]
            if count:
                self._db.execute(
                    """
                    UPDATE inventory_meta
                    SET item_count = item_count + ?, total_bytes = total_bytes + ?
                    WHERE singleton = 1
                    """,
                    (count, total_bytes),
                )
        except BaseException:
            self._db.rollback()
            raise
        else:
            self._db.commit()
        return first, last

    def seal(self) -> None:
        """Permanently prevent subsequent appends; idempotent."""

        with self._db:
            self._db.execute("UPDATE inventory_meta SET sealed = 1 WHERE singleton = 1")

    def page(
        self,
        *,
        after: int = 0,
        limit: int,
        byte_budget: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return the next keyset page, allowing one oversized item alone."""

        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if byte_budget is not None and (
            isinstance(byte_budget, bool)
            or not isinstance(byte_budget, int)
            or byte_budget < 0
        ):
            raise ValueError("byte_budget must be a non-negative integer or None")

        cursor = self._db.execute(
            """
            SELECT ordinal, logical_path, mime, sha256, size, kind, email_format
            FROM inventory_items
            WHERE ordinal > ?
            ORDER BY ordinal
            LIMIT ?
            """,
            (after, limit),
        )
        result: list[dict[str, Any]] = []
        used = 0
        for row in cursor:
            size = int(row["size"])
            if byte_budget is not None and result and used + size > byte_budget:
                break
            logical_path = str(row["logical_path"])
            digest = str(row["sha256"])
            item: dict[str, Any] = {
                "ordinal": int(row["ordinal"]),
                "logical_path": logical_path,
                "filename": PurePosixPath(logical_path).name,
                "mime": str(row["mime"]),
                "sha256": digest,
                "size": size,
                "kind": str(row["kind"]),
                "path": f"files/{digest}",
            }
            if row["email_format"] is not None:
                item["email_format"] = str(row["email_format"])
            result.append(item)
            used += size
        return result

    @staticmethod
    def _validated(
        item: Mapping[str, Any],
    ) -> tuple[str, str, str, int, str, str | None]:
        logical_path = item.get("logical_path")
        mime = item.get("mime")
        digest = item.get("sha256")
        size = item.get("size")
        kind = item.get("kind")
        email_format = item.get("email_format")
        if not isinstance(logical_path, str) or not logical_path:
            raise ValueError("inventory item requires logical_path")
        if not isinstance(mime, str) or not mime:
            raise ValueError("inventory item requires mime")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or digest != digest.lower()
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("inventory item requires lowercase SHA-256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("inventory item requires non-negative size")
        if not isinstance(kind, str) or not kind:
            raise ValueError("inventory item requires kind")
        if email_format is not None and email_format not in {"eml", "mbox"}:
            raise ValueError("inventory email_format must be eml, mbox, or None")
        return logical_path, mime, digest, size, kind, email_format
