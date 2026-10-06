"""Immutable, page-owned prepared text addressed by ordinary stored values."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .value_codec import (
    PreparedContentRef,
    SQLiteValue,
    decode_stored_value,
    repair_unicode_text,
    repair_unicode_value,
)


@dataclass(frozen=True, slots=True)
class PreparedPageDraft:
    page_number: int
    text: str
    positions: Any | None = None


@dataclass(frozen=True, slots=True)
class PreparedPagePin:
    version_id: int
    page_number: int
    content_hash: str
    start: int
    end: int
    positions: Any | None


@dataclass(frozen=True, slots=True)
class PreparedText:
    text: str
    pins: tuple[PreparedPagePin, ...]
    source_artifact_id: int | None
    ref_id: int
    set_id: int
    page_number: int | None


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positions_json(value: Any | None) -> str | None:
    if value is None:
        return None
    repaired = repair_unicode_value(value)
    return json.dumps(
        repaired,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _page_drafts(
    pages: Sequence[PreparedPageDraft],
) -> list[tuple[int, str, str, str | None]]:
    prepared: list[tuple[int, str, str, str | None]] = []
    seen: set[int] = set()
    for page in pages:
        if not isinstance(page, PreparedPageDraft):
            raise TypeError("pages must contain PreparedPageDraft values")
        number = _positive_int(page.page_number, "page_number")
        if number in seen:
            raise ValueError(f"duplicate prepared page {number}")
        if not isinstance(page.text, str):
            raise TypeError("prepared page text must be a string")
        text = repair_unicode_text(page.text)
        digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        prepared.append((number, text, digest, _positions_json(page.positions)))
        seen.add(number)
    if not prepared:
        raise ValueError("a prepared content set requires at least one page")
    prepared.sort(key=lambda item: item[0])
    return prepared


class PreparedContentStore:
    """Write exact prepared-content references inside an existing operation."""

    def __init__(self, project: Any):
        self.project = project

    @property
    def db(self) -> sqlite3.Connection:
        """Return the calling thread's project connection."""

        return self.project.db

    def _require_transaction(self) -> None:
        if not self.db.in_transaction:
            raise RuntimeError("prepared content writes require a caller transaction")

    def _operation(self, producing_op_id: int) -> int:
        op_id = _positive_int(producing_op_id, "producing_op_id")
        row = self.db.execute("SELECT status FROM ops WHERE id=?", (op_id,)).fetchone()
        if row is None:
            raise ValueError(f"operation {op_id} does not exist")
        if str(row[0]) != "applied":
            raise ValueError(f"operation {op_id} is not applied")
        return op_id

    def _artifact(self, source_artifact_id: int) -> tuple[int, int]:
        artifact_id = _positive_int(source_artifact_id, "source_artifact_id")
        row = self.db.execute(
            "SELECT page_count FROM source_artifacts WHERE id=?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"source artifact {artifact_id} does not exist")
        page_count = row[0]
        if type(page_count) is not int or page_count <= 0:
            raise ValueError("source artifact must have a positive page count")
        return artifact_id, page_count

    def _insert_set(
        self,
        *,
        source_artifact_id: int,
        producing_op_id: int,
        pages: list[tuple[int, str, str, str | None]],
        reused: dict[int, int] | None = None,
    ) -> int:
        cursor = self.db.execute(
            "INSERT INTO prepared_content_sets(source_artifact_id,producing_op_id) "
            "VALUES (?,?)",
            (source_artifact_id, producing_op_id),
        )
        set_id = int(cursor.lastrowid)
        versions = dict(reused or {})
        for number, text, digest, positions in pages:
            cursor = self.db.execute(
                "INSERT INTO prepared_page_versions("
                "source_artifact_id,page_number,prepared_text,content_hash,"
                "positions_json,producing_op_id) VALUES (?,?,?,?,?,?)",
                (
                    source_artifact_id,
                    number,
                    text,
                    digest,
                    positions,
                    producing_op_id,
                ),
            )
            versions[number] = int(cursor.lastrowid)
        self.db.executemany(
            "INSERT INTO prepared_content_set_pages(set_id,page_number,version_id) "
            "VALUES (?,?,?)",
            (
                (set_id, number, version_id)
                for number, version_id in sorted(versions.items())
            ),
        )
        return set_id

    def _insert_selector(
        self, set_id: int, page_number: int | None
    ) -> PreparedContentRef:
        cursor = self.db.execute(
            "INSERT INTO prepared_content_refs(set_id,page_number) VALUES (?,?)",
            (set_id, page_number),
        )
        return PreparedContentRef(int(cursor.lastrowid))

    def stage_reference(
        self,
        *,
        source_artifact_id: int,
        producing_op_id: int,
        pages: Sequence[PreparedPageDraft],
        page_number: int | None = None,
    ) -> PreparedContentRef:
        """Create one immutable set and selector without owning the transaction."""

        self._require_transaction()
        op_id = self._operation(producing_op_id)
        artifact_id, page_count = self._artifact(source_artifact_id)
        prepared = _page_drafts(pages)
        selected = (
            None if page_number is None else _positive_int(page_number, "page_number")
        )
        numbers = {page[0] for page in prepared}
        if selected is None:
            if numbers != set(range(1, page_count + 1)):
                raise ValueError(
                    "document prepared content must contain every artifact page"
                )
        elif selected not in numbers:
            raise ValueError(f"prepared content does not contain page {selected}")
        if any(number > page_count for number in numbers):
            raise ValueError("prepared page exceeds the source artifact page count")

        self.db.execute("SAVEPOINT prepared_content_stage")
        try:
            set_id = self._insert_set(
                source_artifact_id=artifact_id,
                producing_op_id=op_id,
                pages=prepared,
            )
            ref = self._insert_selector(set_id, selected)
            self.db.execute("RELEASE prepared_content_stage")
            return ref
        except BaseException:
            self.db.execute("ROLLBACK TO prepared_content_stage")
            self.db.execute("RELEASE prepared_content_stage")
            raise

    def stage_selector(
        self, *, set_id: int, page_number: int | None = None
    ) -> PreparedContentRef:
        """Add another selector for an existing immutable set."""

        self._require_transaction()
        content_set_id = _positive_int(set_id, "set_id")
        selected = (
            None if page_number is None else _positive_int(page_number, "page_number")
        )
        existing = self.db.execute(
            "SELECT id FROM prepared_content_refs WHERE set_id=? AND page_number IS ?",
            (content_set_id, selected),
        ).fetchone()
        if existing is not None:
            return PreparedContentRef(int(existing[0]))
        if selected is None:
            raise ValueError(
                "a document selector must be created with its complete set"
            )
        member = self.db.execute(
            "SELECT 1 FROM prepared_content_set_pages WHERE set_id=? AND page_number=?",
            (content_set_id, selected),
        ).fetchone()
        if member is None:
            raise ValueError(f"prepared content set does not contain page {selected}")
        return self._insert_selector(content_set_id, selected)

    def stage_replacement(
        self,
        *,
        base_ref_id: int,
        producing_op_id: int,
        replacements: Sequence[PreparedPageDraft],
        page_number: int | None = None,
    ) -> PreparedContentRef:
        """Create a new set, reusing every page version not replaced."""

        self._require_transaction()
        op_id = self._operation(producing_op_id)
        base_id = _positive_int(base_ref_id, "base_ref_id")
        base = self.db.execute(
            "SELECT ref.set_id,set_record.source_artifact_id "
            "FROM prepared_content_refs AS ref "
            "JOIN prepared_content_sets AS set_record ON set_record.id=ref.set_id "
            "WHERE ref.id=?",
            (base_id,),
        ).fetchone()
        if base is None:
            raise ValueError(f"prepared content reference {base_id} does not exist")
        if base[1] is None:
            raise ValueError(
                "cannot derive new content after its source artifact was deleted"
            )
        artifact_id, page_count = self._artifact(int(base[1]))
        members = {
            int(row[0]): int(row[1])
            for row in self.db.execute(
                "SELECT page_number,version_id FROM prepared_content_set_pages "
                "WHERE set_id=? ORDER BY page_number",
                (int(base[0]),),
            )
        }
        prepared = _page_drafts(replacements)
        replacement_numbers = {page[0] for page in prepared}
        if not replacement_numbers <= members.keys():
            raise ValueError("replacement pages must already belong to the content set")
        selected = (
            None if page_number is None else _positive_int(page_number, "page_number")
        )
        if selected is not None and selected not in members:
            raise ValueError(f"prepared content does not contain page {selected}")
        if selected is None and set(members) != set(range(1, page_count + 1)):
            raise ValueError(
                "document prepared content must contain every artifact page"
            )
        reused = {
            number: version
            for number, version in members.items()
            if number not in replacement_numbers
        }

        self.db.execute("SAVEPOINT prepared_content_stage")
        try:
            set_id = self._insert_set(
                source_artifact_id=artifact_id,
                producing_op_id=op_id,
                pages=prepared,
                reused=reused,
            )
            ref = self._insert_selector(set_id, selected)
            self.db.execute("RELEASE prepared_content_stage")
            return ref
        except BaseException:
            self.db.execute("ROLLBACK TO prepared_content_stage")
            self.db.execute("RELEASE prepared_content_stage")
            raise

    def resolve(self, ref_id: int) -> PreparedText:
        return self.resolve_many([ref_id])[_positive_int(ref_id, "ref_id")]

    def resolve_many(self, ref_ids: Sequence[int]) -> dict[int, PreparedText]:
        ids = list(dict.fromkeys(_positive_int(value, "ref_id") for value in ref_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.db.execute(
            "SELECT ref.id,ref.set_id,set_record.source_artifact_id,ref.page_number,"
            "member.page_number,version.id,version.prepared_text,version.content_hash,"
            "version.positions_json "
            "FROM prepared_content_refs AS ref "
            "JOIN prepared_content_sets AS set_record ON set_record.id=ref.set_id "
            "JOIN prepared_content_set_pages AS member ON member.set_id=ref.set_id "
            "AND (ref.page_number IS NULL OR member.page_number=ref.page_number) "
            "JOIN prepared_page_versions AS version ON version.id=member.version_id "
            f"WHERE ref.id IN ({placeholders}) ORDER BY ref.id,member.page_number",
            ids,
        ).fetchall()
        grouped: dict[int, list[Any]] = {}
        for row in rows:
            grouped.setdefault(int(row[0]), []).append(row)
        missing = [ref_id for ref_id in ids if ref_id not in grouped]
        if missing:
            raise KeyError(f"prepared content reference not found: {missing[0]}")

        resolved: dict[int, PreparedText] = {}
        for ref_id in ids:
            pages = grouped[ref_id]
            text_parts: list[str] = []
            pins: list[PreparedPagePin] = []
            offset = 0
            for index, row in enumerate(pages):
                if index:
                    text_parts.append("\n\n")
                    offset += 2
                text = str(row[6])
                start = offset
                text_parts.append(text)
                offset += len(text)
                positions = json.loads(str(row[8])) if row[8] is not None else None
                pins.append(
                    PreparedPagePin(
                        version_id=int(row[5]),
                        page_number=int(row[4]),
                        content_hash=str(row[7]),
                        start=start,
                        end=offset,
                        positions=positions,
                    )
                )
            first = pages[0]
            resolved[ref_id] = PreparedText(
                text="".join(text_parts),
                pins=tuple(pins),
                source_artifact_id=(int(first[2]) if first[2] is not None else None),
                ref_id=ref_id,
                set_id=int(first[1]),
                page_number=(int(first[3]) if first[3] is not None else None),
            )
        return resolved


def decode_logical_value(
    db: sqlite3.Connection,
    value_kind: str | None,
    stored_value: SQLiteValue,
    *,
    tolerate_errors: bool = False,
) -> Any:
    """Decode an authority value, resolving internal prepared-text locators."""

    decoded = decode_stored_value(
        value_kind, stored_value, tolerate_errors=tolerate_errors
    )
    if not isinstance(decoded, PreparedContentRef):
        return decoded
    row = db.execute(
        "SELECT value FROM prepared_content_ref_values WHERE ref_id=?",
        (decoded.ref_id,),
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        if tolerate_errors:
            return None
        raise ValueError(f"prepared content reference {decoded.ref_id} is unavailable")
    return row[0]


__all__ = [
    "PreparedContentStore",
    "PreparedPageDraft",
    "PreparedPagePin",
    "PreparedText",
    "decode_logical_value",
]
