"""Bounded SQLite occurrence inventory for precommit PDF intake."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from frisket.engine.store.import_inventory import ImportInventory


class PdfDraftInventory:
    """Extend the shared import inventory with a declared-before-copy roster."""

    def __init__(self, path: Path) -> None:
        self.base = ImportInventory(path)
        self.db = self.base._db
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS pdf_selected (
              ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
              occurrence_id TEXT NOT NULL UNIQUE,
              logical_path TEXT NOT NULL,
              filename TEXT NOT NULL,
              size INTEGER NOT NULL CHECK(size >= 0),
              sample_key TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS pdf_selected_state (
              occurrence_id TEXT PRIMARY KEY REFERENCES pdf_selected(occurrence_id),
              upload_state TEXT NOT NULL DEFAULT 'pending',
              analysis_state TEXT NOT NULL DEFAULT 'pending',
              staged_ordinal INTEGER UNIQUE,
              sha256 TEXT,
              page_count INTEGER,
              category TEXT NOT NULL DEFAULT 'unchecked',
              sample_pages TEXT NOT NULL DEFAULT '[]',
              error TEXT,
              matched INTEGER NOT NULL DEFAULT 0 CHECK(matched IN (0,1))
            );
            CREATE TABLE IF NOT EXISTS pdf_manifest_batches (
              batch_id TEXT PRIMARY KEY,
              payload_hash TEXT NOT NULL,
              through_ordinal INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pdf_upload_batches (
              batch_id TEXT PRIMARY KEY,
              payload_hash TEXT NOT NULL,
              through_ordinal INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pdf_stats (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1),
              selected_count INTEGER NOT NULL DEFAULT 0,
              selected_bytes INTEGER NOT NULL DEFAULT 0,
              staged_count INTEGER NOT NULL DEFAULT 0,
              staged_bytes INTEGER NOT NULL DEFAULT 0,
              analyzed_count INTEGER NOT NULL DEFAULT 0,
              text_count INTEGER NOT NULL DEFAULT 0,
              suspicious_count INTEGER NOT NULL DEFAULT 0,
              missing_count INTEGER NOT NULL DEFAULT 0,
              unreadable_count INTEGER NOT NULL DEFAULT 0,
              manifest_sealed INTEGER NOT NULL DEFAULT 0 CHECK(manifest_sealed IN (0,1))
            );
            INSERT OR IGNORE INTO pdf_stats(singleton) VALUES(1);
            CREATE TABLE IF NOT EXISTS pdf_ocr_cache (
              sha256 TEXT NOT NULL,
              page INTEGER NOT NULL,
              engine TEXT NOT NULL,
              settings_hash TEXT NOT NULL,
              payload TEXT NOT NULL,
              PRIMARY KEY(sha256,page,engine,settings_hash)
            );
            CREATE TABLE IF NOT EXISTS pdf_commit_projection (
              commit_ordinal INTEGER PRIMARY KEY,
              selected_ordinal INTEGER NOT NULL UNIQUE,
              occurrence_id TEXT NOT NULL UNIQUE
            );
            CREATE INDEX IF NOT EXISTS idx_pdf_selected_sample
              ON pdf_selected_state(category,analysis_state,occurrence_id);
            CREATE INDEX IF NOT EXISTS idx_pdf_selected_path_size
              ON pdf_selected(logical_path,size);
            CREATE INDEX IF NOT EXISTS idx_pdf_selected_filename_size
              ON pdf_selected(filename,size);
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.base.close()

    def __enter__(self) -> PdfDraftInventory:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @staticmethod
    def payload_hash(items: Iterable[Mapping[str, Any]]) -> str:
        return hashlib.sha256(
            json.dumps(
                [dict(item) for item in items],
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()

    def append_manifest_batch(
        self,
        batch_id: str,
        items: list[Mapping[str, Any]],
        *,
        expected_files: int,
        expected_bytes: int,
    ) -> tuple[int, bool]:
        payload_hash = self.payload_hash(items)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute(
                "SELECT payload_hash,through_ordinal FROM pdf_manifest_batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise ValueError("batch_id was already used for different files")
                self.db.commit()
                return int(existing["through_ordinal"]), False
            if self.stats()["manifest_sealed"]:
                raise RuntimeError("PDF selection manifest is sealed")
            current = self.stats()
            added_bytes = sum(int(item["size"]) for item in items)
            if (
                current["selected_count"] + len(items) > expected_files
                or current["selected_bytes"] + added_bytes > expected_bytes
            ):
                raise OverflowError("PDF selection exceeds declared totals")
            total_bytes = 0
            through = 0
            for raw in items:
                occurrence_id = str(raw["occurrence_id"])
                logical_path = str(raw["logical_path"])
                filename = str(raw["filename"])
                size = int(raw["size"])
                sample_key = hashlib.sha256(
                    f"{logical_path.rpartition('/')[0]}\0{occurrence_id}".encode()
                ).hexdigest()
                cursor = self.db.execute(
                    "INSERT INTO pdf_selected(occurrence_id,logical_path,filename,size,sample_key) "
                    "VALUES(?,?,?,?,?)",
                    (occurrence_id, logical_path, filename, size, sample_key),
                )
                through = int(cursor.lastrowid)
                self.db.execute(
                    "INSERT INTO pdf_selected_state(occurrence_id) VALUES(?)",
                    (occurrence_id,),
                )
                total_bytes += size
            self.db.execute(
                "UPDATE pdf_stats SET selected_count=selected_count+?,"
                "selected_bytes=selected_bytes+? WHERE singleton=1",
                (len(items), total_bytes),
            )
            self.db.execute(
                "INSERT INTO pdf_manifest_batches(batch_id,payload_hash,through_ordinal) "
                "VALUES(?,?,?)",
                (batch_id, payload_hash, through),
            )
            self.db.commit()
            return through, True
        except sqlite3.IntegrityError as exc:
            self.db.rollback()
            raise ValueError("PDF occurrence id was already selected") from exc
        except BaseException:
            self.db.rollback()
            raise

    def seal_manifest(self) -> None:
        with self.db:
            self.db.execute("UPDATE pdf_stats SET manifest_sealed=1 WHERE singleton=1")

    def stats(self) -> dict[str, int]:
        row = self.db.execute("SELECT * FROM pdf_stats WHERE singleton=1").fetchone()
        return {key: int(row[key]) for key in row.keys()}

    def selected_through(self) -> int:
        row = self.db.execute(
            "SELECT COALESCE(MAX(ordinal),0) value FROM pdf_selected"
        ).fetchone()
        return int(row["value"])

    def selected(self, occurrence_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT s.*,st.upload_state,st.analysis_state,st.staged_ordinal,st.sha256,"
            "st.page_count,st.category,st.sample_pages,st.error,st.matched "
            "FROM pdf_selected s JOIN pdf_selected_state st USING(occurrence_id) "
            "WHERE s.occurrence_id=?",
            (occurrence_id,),
        ).fetchone()
        return self._row(row) if row is not None else None

    def reconcile_reselection(
        self, files: Iterable[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        candidates: dict[tuple[str, str, int], list[Any]] = {}
        result = []
        for item in files:
            key = (
                str(item["logical_path"]),
                str(item["filename"]),
                int(item["size"]),
            )
            rows = candidates.get(key)
            if rows is None:
                rows = list(
                    self.db.execute(
                        "SELECT s.occurrence_id,st.upload_state FROM pdf_selected s "
                        "JOIN pdf_selected_state st USING(occurrence_id) "
                        "WHERE s.logical_path=? AND s.filename=? AND s.size=? "
                        "ORDER BY s.ordinal",
                        key,
                    )
                )
                candidates[key] = rows
            row = rows.pop(0) if rows else None
            result.append(
                {
                    "logical_path": item["logical_path"],
                    "filename": item["filename"],
                    "size": item["size"],
                    "matched": row is not None,
                    "occurrence_id": str(row["occurrence_id"])
                    if row is not None
                    else None,
                    "upload_needed": bool(
                        row is not None and row["upload_state"] == "pending"
                    ),
                }
            )
        return result

    def stage_batch(
        self,
        batch_id: str,
        facts: list[Mapping[str, Any]],
    ) -> tuple[int, bool]:
        payload_hash = self.payload_hash(facts)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute(
                "SELECT payload_hash,through_ordinal FROM pdf_upload_batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise ValueError("batch_id was already used for different files")
                self.db.commit()
                return int(existing["through_ordinal"]), False
            through = 0
            staged_bytes = 0
            for fact in facts:
                occurrence_id = str(fact["occurrence_id"])
                selected = self.db.execute(
                    "SELECT s.logical_path,s.size,st.upload_state FROM pdf_selected s "
                    "JOIN pdf_selected_state st USING(occurrence_id) WHERE occurrence_id=?",
                    (occurrence_id,),
                ).fetchone()
                if selected is None:
                    raise ValueError("upload occurrence is not selected")
                if selected["upload_state"] != "pending":
                    raise ValueError("selected PDF was already uploaded")
                if (
                    selected["logical_path"] != fact["logical_path"]
                    or int(selected["size"]) != fact["size"]
                ):
                    raise ValueError("uploaded PDF changed after selection")
                values = ImportInventory._validated(
                    {
                        "logical_path": fact["logical_path"],
                        "mime": "application/pdf",
                        "sha256": fact["sha256"],
                        "size": fact["size"],
                        "kind": "files",
                    }
                )
                cursor = self.db.execute(
                    "INSERT INTO inventory_items(logical_path,mime,sha256,size,kind,email_format) "
                    "VALUES(?,?,?,?,?,?)",
                    values,
                )
                through = int(cursor.lastrowid)
                self.db.execute(
                    "UPDATE pdf_selected_state SET upload_state='staged',"
                    "staged_ordinal=?,sha256=? "
                    "WHERE occurrence_id=?",
                    (
                        through,
                        fact["sha256"],
                        occurrence_id,
                    ),
                )
                staged_bytes += int(fact["size"])
            self.db.execute(
                "UPDATE inventory_meta SET item_count=item_count+?,total_bytes=total_bytes+?,"
                "through_ordinal=? WHERE singleton=1",
                (len(facts), staged_bytes, through),
            )
            self.db.execute(
                "UPDATE pdf_stats SET staged_count=staged_count+?,"
                "staged_bytes=staged_bytes+? WHERE singleton=1",
                (len(facts), staged_bytes),
            )
            self.db.execute(
                "INSERT INTO pdf_upload_batches(batch_id,payload_hash,through_ordinal) VALUES(?,?,?)",
                (batch_id, payload_hash, through),
            )
            self.db.commit()
            return through, True
        except BaseException:
            self.db.rollback()
            raise

    def complete_analysis(
        self, occurrence_id: str, analysis: Mapping[str, Any]
    ) -> bool:
        category = str(analysis["category"])
        category_column = {
            "text": "text_count",
            "suspicious": "suspicious_count",
            "missing": "missing_count",
            "unreadable": "unreadable_count",
        }.get(category)
        if category_column is None:
            raise ValueError("invalid PDF analysis category")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT upload_state,analysis_state FROM pdf_selected_state "
                "WHERE occurrence_id=?",
                (occurrence_id,),
            ).fetchone()
            if (
                row is None
                or row["upload_state"] != "staged"
                or row["analysis_state"] != "pending"
            ):
                self.db.commit()
                return False
            self.db.execute(
                "UPDATE pdf_selected_state SET analysis_state=?,page_count=?,category=?,"
                "sample_pages=?,error=? WHERE occurrence_id=?",
                (
                    analysis["analysis_state"],
                    analysis.get("page_count"),
                    category,
                    json.dumps(analysis.get("sample_pages") or []),
                    analysis.get("error"),
                    occurrence_id,
                ),
            )
            self.db.execute(
                "UPDATE pdf_stats SET analyzed_count=analyzed_count+1,"
                f"{category_column}={category_column}+1 WHERE singleton=1"
            )
            self.db.commit()
            return True
        except BaseException:
            self.db.rollback()
            raise

    def upload_batch_through(
        self, batch_id: str, facts: list[Mapping[str, Any]]
    ) -> int | None:
        row = self.db.execute(
            "SELECT payload_hash,through_ordinal FROM pdf_upload_batches WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != self.payload_hash(facts):
            raise ValueError("batch_id was already used for different files")
        return int(row["through_ordinal"])

    def samples(
        self, category: str, *, after: str, limit: int
    ) -> tuple[list[dict[str, Any]], str | None]:
        rows = list(
            self.db.execute(
                "SELECT s.*,st.analysis_state,st.page_count,st.category,st.sample_pages,st.error "
                "FROM pdf_selected s JOIN pdf_selected_state st USING(occurrence_id) "
                "WHERE st.category=? AND s.sample_key>? ORDER BY s.sample_key LIMIT ?",
                (category, after, limit + 1),
            )
        )
        more = len(rows) > limit
        rows = rows[:limit]
        return [self._row(row) for row in rows], (
            str(rows[-1]["sample_key"]) if more else None
        )

    def staged_page(self, *, after: int, limit: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT s.*,st.upload_state,st.analysis_state,st.staged_ordinal,st.sha256,"
            "st.page_count,st.category,st.sample_pages,st.error,st.matched "
            "FROM pdf_selected s JOIN pdf_selected_state st USING(occurrence_id) "
            "WHERE s.ordinal>? ORDER BY s.ordinal LIMIT ?",
            (after, limit),
        )
        return [self._row(row) for row in rows]

    def initialize_commit_projection(self) -> int:
        """Freeze all unmatched selected occurrences into contiguous commit order."""

        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute(
                "SELECT COUNT(*) value FROM pdf_commit_projection"
            ).fetchone()
            if int(existing["value"]):
                total = int(existing["value"])
            else:
                commit_ordinal = 0
                cursor = self.db.execute(
                    "SELECT s.ordinal,s.occurrence_id FROM pdf_selected s "
                    "JOIN pdf_selected_state st USING(occurrence_id) "
                    "WHERE st.matched=0 ORDER BY s.ordinal"
                )
                for row in cursor:
                    commit_ordinal += 1
                    self.db.execute(
                        "INSERT INTO pdf_commit_projection"
                        "(commit_ordinal,selected_ordinal,occurrence_id) VALUES(?,?,?)",
                        (
                            commit_ordinal,
                            int(row["ordinal"]),
                            str(row["occurrence_id"]),
                        ),
                    )
                total = commit_ordinal
            self.db.commit()
            return total
        except BaseException:
            self.db.rollback()
            raise

    def projection_page(self, *, after: int, limit: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT p.commit_ordinal,p.selected_ordinal,s.*,st.upload_state,"
            "st.analysis_state,st.staged_ordinal,st.sha256,st.page_count,st.category,"
            "st.sample_pages,st.error,st.matched FROM pdf_commit_projection p "
            "JOIN pdf_selected s ON s.ordinal=p.selected_ordinal "
            "JOIN pdf_selected_state st USING(occurrence_id) "
            "WHERE p.commit_ordinal>? ORDER BY p.commit_ordinal LIMIT ?",
            (after, limit),
        )
        return [self._row(row) for row in rows]

    def mark_matches(self, occurrence_ids: list[str]) -> None:
        with self.db:
            self.db.executemany(
                "UPDATE pdf_selected_state SET matched=1 WHERE occurrence_id=?",
                [(value,) for value in occurrence_ids],
            )

    def clear_matches(self) -> None:
        with self.db:
            self.db.execute("UPDATE pdf_selected_state SET matched=0")

    def match_project_values(
        self,
        project_db_path: Path,
        *,
        sheet_id: int,
        filename_column_id: int,
        size_column_id: int,
    ) -> None:
        """Match the full roster with one current-value SQL projection pass."""

        self.clear_matches()
        self.db.execute(
            "ATTACH DATABASE ? AS pdf_import_project", (str(project_db_path),)
        )
        try:
            with self.db:
                self.db.execute(
                    "UPDATE pdf_selected_state AS state SET matched=1 "
                    "WHERE state.occurrence_id IN ("
                    "SELECT selected.occurrence_id "
                    "FROM pdf_import_project.rows AS project_row "
                    "JOIN pdf_import_project.current_cell_values AS filename "
                    "ON filename.row_id=project_row.id AND filename.column_id=? "
                    "AND filename.validity='valid' "
                    "JOIN pdf_import_project.current_cell_values AS size "
                    "ON size.row_id=project_row.id AND size.column_id=? "
                    "AND size.validity='valid' "
                    "JOIN pdf_selected AS selected "
                    "ON selected.filename=filename.value AND selected.size=size.value "
                    "WHERE project_row.sheet_id=? AND project_row.hidden=0)",
                    (filename_column_id, size_column_id, sheet_id),
                )
        finally:
            self.db.execute("DETACH DATABASE pdf_import_project")

    def match_summary(self) -> tuple[int, dict[str, int]]:
        matched = self.db.execute(
            "SELECT COUNT(*) value FROM pdf_selected_state WHERE matched=1"
        ).fetchone()
        categories = {
            str(row["category"]): int(row["value"])
            for row in self.db.execute(
                "SELECT category,COUNT(*) value FROM pdf_selected_state "
                "WHERE matched=0 GROUP BY category"
            )
        }
        return int(matched["value"]), categories

    def ocr_scope_hash(self, policy: str) -> tuple[str, int, int]:
        """Hash exact ordered staged OCR facts without materializing the roster."""

        digest = hashlib.sha256()
        count = pages = 0
        cursor = self.db.execute(
            "SELECT s.ordinal,s.occurrence_id,st.sha256,st.page_count,st.category "
            "FROM pdf_selected s JOIN pdf_selected_state st USING(occurrence_id) "
            "WHERE st.upload_state='staged' AND st.matched=0 ORDER BY s.ordinal"
        )
        for row in cursor:
            selected = policy == "all" or (
                policy == "missing_or_suspicious"
                and row["category"] in {"missing", "suspicious"}
            )
            if not selected:
                continue
            fact = {
                "ordinal": int(row["ordinal"]),
                "occurrence_id": str(row["occurrence_id"]),
                "sha256": str(row["sha256"]),
                "page_count": int(row["page_count"] or 0),
            }
            digest.update(
                json.dumps(fact, sort_keys=True, separators=(",", ":")).encode()
            )
            digest.update(b"\n")
            count += 1
            pages += fact["page_count"]
        return digest.hexdigest(), count, pages

    def cache_ocr(
        self,
        sha256: str,
        page: int,
        engine: str,
        settings_hash: str,
        payload: Mapping[str, Any],
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO pdf_ocr_cache"
                "(sha256,page,engine,settings_hash,payload) VALUES(?,?,?,?,?)",
                (
                    sha256,
                    page,
                    engine,
                    settings_hash,
                    json.dumps(dict(payload), separators=(",", ":")),
                ),
            )

    def cached_ocr(
        self,
        sha256: str,
        page: int,
        engine: str | None = None,
        settings_hash: str | None = None,
    ) -> dict[str, Any] | None:
        clauses = ["sha256=?", "page=?"]
        values: list[Any] = [sha256, page]
        if engine is not None:
            clauses.append("engine=?")
            values.append(engine)
        if settings_hash is not None:
            clauses.append("settings_hash=?")
            values.append(settings_hash)
        row = self.db.execute(
            "SELECT payload FROM pdf_ocr_cache WHERE "
            + " AND ".join(clauses)
            + " ORDER BY rowid DESC LIMIT 1",
            values,
        ).fetchone()
        return json.loads(row["payload"]) if row is not None else None

    @staticmethod
    def _row(row) -> dict[str, Any]:
        value = {key: row[key] for key in row.keys()}
        if "sample_pages" in value:
            value["sample_pages"] = json.loads(value["sample_pages"])
        for key in ("matched",):
            if key in value:
                value[key] = bool(value[key])
        return value


__all__ = ["PdfDraftInventory"]
