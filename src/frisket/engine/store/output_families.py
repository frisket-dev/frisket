"""Atomic creation and reuse of generated output-column families."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from frisket.authoring import column_types


class OutputFamilyStore:
    """Apply MapRunner's ordinary column rules to a whole output set at once."""

    def __init__(self, project: Any):
        self.project = project

    @property
    def db(self) -> Any:
        """The CALLING thread's connection, resolved per access — see
        ``RunResultStore.db``: capturing ``project.db`` in ``__init__`` hands
        every thread the CONSTRUCTING thread's connection, which races
        sqlite3's statement cache."""
        return self.project.db

    def create_or_reuse(
        self,
        *,
        sheet_id: int,
        fields: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, int], list[int]]:
        """Create or exactly reuse every declared output in one transaction.

        A present family must already be complete, visible, generated, and
        type/format-identical. This primitive never overwrites user columns,
        revives hidden columns, or coerces an older generated schema.
        """

        normalized = [dict(field) for field in fields]
        if not normalized:
            raise ValueError("generated output family must not be empty")
        names = [str(field.get("name") or "") for field in normalized]
        if any(not name for name in names):
            raise ValueError("generated output names must be non-empty")
        if len(names) != len(set(names)):
            raise ValueError("generated output names must be unique")
        for field in normalized:
            column_type = str(field.get("column_type") or "")
            if not column_types.is_registered(column_type):
                raise ValueError(f"unknown column type: {column_type}")

        owns_transaction = not self.db.in_transaction
        try:
            if owns_transaction:
                self.db.execute("BEGIN IMMEDIATE")

            placeholders = ",".join("?" for _ in names)
            existing_rows = (
                self.db.execute(
                    "SELECT * FROM columns WHERE sheet_id=? "
                    f"AND name IN ({placeholders})",
                    (sheet_id, *names),
                ).fetchall()
                if names
                else []
            )
            existing = {str(row["name"]): row for row in existing_rows}

            if existing:
                if len(existing) != len(normalized):
                    raise ValueError(
                        "output columns are not a complete reusable generated family"
                    )
                output_ids: dict[str, int] = {}
                for field in normalized:
                    name = str(field["name"])
                    row = existing[name]
                    if (
                        bool(row["hidden"])
                        or not bool(row["ai_generated"])
                        or str(row["type"]) != str(field["column_type"])
                        or row["format"] != field.get("format")
                    ):
                        raise ValueError(
                            "output columns are not an exact reusable generated family"
                        )
                    output_ids[name] = int(row["id"])
                if owns_transaction:
                    self.db.commit()
                return output_ids, []

            position_row = self.db.execute(
                "SELECT COALESCE(MAX(position),0) AS position "
                "FROM columns WHERE sheet_id=?",
                (sheet_id,),
            ).fetchone()
            position = int(position_row["position"] or 0)

            output_ids: dict[str, int] = {}
            created: list[int] = []
            for field in normalized:
                name = str(field["name"])
                column_type = str(field["column_type"])
                fmt = field.get("format")
                hidden = bool(field.get("hidden", False))
                default_hidden = bool(field.get("default_hidden", False))

                position += 1
                inserted = self.db.execute(
                    "INSERT INTO columns "
                    "(sheet_id,name,type,ai_generated,format,hidden,"
                    "default_hidden,position) VALUES (?,?,?,1,?,?,?,?)",
                    (
                        sheet_id,
                        name,
                        column_type,
                        fmt,
                        int(hidden),
                        int(default_hidden),
                        position,
                    ),
                )
                column_id = int(inserted.lastrowid)
                output_ids[name] = column_id
                created.append(column_id)

            if owns_transaction:
                self.db.commit()
            return output_ids, created
        except BaseException:
            if owns_transaction and self.db.in_transaction:
                self.db.rollback()
            raise
