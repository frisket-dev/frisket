"""Small, native-SQL persistence comparison slice.

This module deliberately owns only the fixed document/extraction shape used by
the comparison.  It is not a production storage abstraction.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from frisket.engine.store.text_quote_match import quote_ranges


@dataclass(frozen=True)
class Document:
    row_id: int
    title: str
    category: str
    raw_amount: str | None
    text: str
    asset: bytes = b""


@dataclass(frozen=True)
class Extraction:
    row_id: int
    source_version: str
    amount_cents: int | None
    quotes: tuple[str, ...] = ()


class Conflict(RuntimeError):
    pass


class InvalidCitation(ValueError):
    pass


_TABLES = (
    "documents",
    "sources",
    "staged_documents",
    "results",
    "generated_heads",
    "manual_heads",
    "review_status",
    "citations",
    "receipts",
)
_TABLE_COLUMNS = {
    "documents": (
        "row_id",
        "title",
        "category",
        "raw_amount",
        "parsed_amount",
        "amount_state",
        "source_version",
        "asset_hash",
    ),
    "sources": (
        "source_version",
        "row_id",
        "title",
        "category",
        "raw_amount",
        "text",
        "asset_hash",
    ),
    "staged_documents": (
        "operation_id",
        "ordinal",
        "row_id",
        "title",
        "category",
        "raw_amount",
        "parsed_amount",
        "amount_state",
        "text",
        "asset_hash",
        "source_version",
    ),
    "results": ("version_id", "row_id", "source_version", "amount_cents", "kind"),
    "generated_heads": ("row_id", "version_id"),
    "manual_heads": ("row_id", "version_id"),
    "review_status": ("row_id", "decision", "version_id"),
    "citations": (
        "citation_id",
        "row_id",
        "version_id",
        "source_version",
        "start_offset",
        "end_offset",
        "quote",
    ),
    "receipts": ("operation_id", "operation_type", "payload_digest", "receipt_id"),
}
_COLUMN_TYPES = {
    "documents": ("i64", "text", "text", "text", "i64", "text", "text", "text"),
    "sources": ("text", "i64", "text", "text", "text", "text", "text"),
    "staged_documents": (
        "text",
        "i64",
        "i64",
        "text",
        "text",
        "text",
        "i64",
        "text",
        "text",
        "text",
        "text",
    ),
    "results": ("text", "i64", "text", "i64", "text"),
    "generated_heads": ("i64", "text"),
    "manual_heads": ("i64", "text"),
    "review_status": ("i64", "text", "text"),
    "citations": ("text", "i64", "text", "text", "i64", "i64", "text"),
    "receipts": ("text", "text", "text", "text"),
}


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _identifier(kind: str, *parts: object) -> str:
    return f"{kind}-{_digest([kind, *parts])[:24]}"


def _parsed_amount(raw: str | None) -> tuple[int | None, str]:
    if raw is None or not raw.strip():
        return None, "missing"
    cleaned = raw.strip().replace(",", "").replace("$", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None, "invalid"
    if value.is_nan() or value.is_infinite() or value.as_tuple().exponent < -2:
        return None, "invalid"
    cents = int(value * 100)
    if not -(2**63) <= cents < 2**63:
        return None, "invalid"
    return cents, "valid"


def _valid_cents(value: int | None) -> bool:
    return value is None or (type(value) is int and -(2**63) <= value < 2**63)


def _file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(64 * 1024):
            hasher.update(block)
    return hasher.hexdigest()


class ProjectStore:
    """A one-process project store with native SQLite or DuckDB connections."""

    def __init__(
        self,
        path: Path,
        engine: str,
        *,
        checkpoint: Callable[[str], None] | None = None,
    ):
        if engine not in {"sqlite", "duckdb"}:
            raise ValueError("engine must be sqlite or duckdb")
        self.path = Path(path)
        self.engine = engine
        self.checkpoint = checkpoint
        self._lock = threading.RLock()
        self._import_lock = threading.Lock()
        self._local = threading.local()
        self._connections: list[object] = []
        self._closed = False
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "assets").mkdir(exist_ok=True)
        db_path = self.path / ("slice.sqlite" if engine == "sqlite" else "slice.duckdb")
        self._db_path = db_path
        self._create_schema()

    def __enter__(self) -> "ProjectStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            for connection in self._connections:
                connection.close()
            self._connections.clear()
            self._closed = True

    @property
    def conn(self):
        if self._closed:
            raise RuntimeError("ProjectStore is closed")
        connection = getattr(self._local, "connection", None)
        if connection is None:
            if self.engine == "sqlite":
                connection = sqlite3.connect(
                    self._db_path, isolation_level=None, check_same_thread=False
                )
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA journal_mode = WAL")
            else:
                import duckdb

                connection = duckdb.connect(str(self._db_path))
            self._local.connection = connection
            with self._lock:
                self._connections.append(connection)
        return connection

    def _create_schema(self) -> None:
        statements = [
            "CREATE TABLE IF NOT EXISTS documents (row_id BIGINT PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL, raw_amount TEXT, parsed_amount BIGINT, amount_state TEXT NOT NULL, source_version TEXT NOT NULL, asset_hash TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS sources (source_version TEXT PRIMARY KEY, row_id BIGINT NOT NULL, title TEXT NOT NULL, category TEXT NOT NULL, raw_amount TEXT, text TEXT NOT NULL, asset_hash TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS staged_documents (operation_id TEXT NOT NULL, ordinal BIGINT NOT NULL, row_id BIGINT NOT NULL, title TEXT NOT NULL, category TEXT NOT NULL, raw_amount TEXT, parsed_amount BIGINT, amount_state TEXT NOT NULL, text TEXT NOT NULL, asset_hash TEXT NOT NULL, source_version TEXT NOT NULL, PRIMARY KEY(operation_id, ordinal))",
            "CREATE TABLE IF NOT EXISTS results (version_id TEXT PRIMARY KEY, row_id BIGINT NOT NULL, source_version TEXT NOT NULL, amount_cents BIGINT, kind TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS generated_heads (row_id BIGINT PRIMARY KEY, version_id TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS manual_heads (row_id BIGINT PRIMARY KEY, version_id TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS review_status (row_id BIGINT PRIMARY KEY, decision TEXT NOT NULL, version_id TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS citations (citation_id TEXT PRIMARY KEY, row_id BIGINT NOT NULL, version_id TEXT NOT NULL, source_version TEXT NOT NULL, start_offset BIGINT NOT NULL, end_offset BIGINT NOT NULL, quote TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS receipts (operation_id TEXT PRIMARY KEY, operation_type TEXT NOT NULL, payload_digest TEXT NOT NULL, receipt_id TEXT NOT NULL)",
        ]
        for statement in statements:
            self.conn.execute(statement)

    @contextlib.contextmanager
    def _tx(self, before_commit: str | None = None) -> Iterator[None]:
        with self._lock:
            self.conn.execute(
                "BEGIN IMMEDIATE" if self.engine == "sqlite" else "BEGIN TRANSACTION"
            )
            try:
                yield
                if before_commit:
                    self._checkpoint(before_commit)
                self.conn.execute("COMMIT")
            except BaseException:
                try:
                    self.conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    @contextlib.contextmanager
    def _snapshot(self) -> Iterator[None]:
        """A read transaction pins tables while the logical stream is emitted."""
        self.conn.execute("BEGIN TRANSACTION" if self.engine == "duckdb" else "BEGIN")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def _rows(self, sql: str, params: Sequence[object] = ()) -> list[dict]:
        cursor = self.conn.execute(sql, list(params))
        names = [part[0] for part in cursor.description]
        return [dict(zip(names, values)) for values in cursor.fetchall()]

    def _one(self, sql: str, params: Sequence[object] = ()) -> dict | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def _checkpoint(self, name: str) -> None:
        if self.checkpoint:
            self.checkpoint(name)

    def _receipt(
        self, operation_id: str, operation_type: str, digest: str
    ) -> str | None:
        found = self._one(
            "SELECT * FROM receipts WHERE operation_id=?", (operation_id,)
        )
        if not found:
            return None
        if (
            found["operation_type"] != operation_type
            or found["payload_digest"] != digest
        ):
            raise Conflict(
                f"operation {operation_id!r} was already used with different payload"
            )
        return found["receipt_id"]

    def _insert_receipt(
        self, operation_id: str, operation_type: str, digest: str
    ) -> str:
        receipt = _identifier("receipt", operation_type, operation_id, digest)
        self.conn.execute(
            "INSERT INTO receipts VALUES (?, ?, ?, ?)",
            (operation_id, operation_type, digest, receipt),
        )
        return receipt

    def _asset(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        destination = self.path / "assets" / digest
        if not destination.exists():
            temporary = destination.with_suffix(".tmp-" + uuid.uuid4().hex)
            with temporary.open("wb") as out:
                for offset in range(0, len(data), 64 * 1024):
                    out.write(data[offset : offset + 64 * 1024])
            temporary.replace(destination)
        return digest

    def _insert_many(
        self, table: str, width: int, rows: Sequence[Sequence[object]]
    ) -> None:
        """Bound driver crossings as well as transaction scope for both engines."""
        if not rows:
            return
        if self.engine == "duckdb":
            import pyarrow as pa

            columns = _TABLE_COLUMNS[table]
            if width != len(columns):
                raise ValueError("fixed schema width mismatch")
            arrow_types = {"i64": pa.int64(), "text": pa.string()}
            for start in range(0, len(rows), 500):
                group = rows[start : start + 500]
                arrow = pa.table(
                    {
                        column: pa.array(
                            [row[index] for row in group],
                            type=arrow_types[_COLUMN_TYPES[table][index]],
                        )
                        for index, column in enumerate(columns)
                    }
                )
                name = "slice_batch_" + uuid.uuid4().hex
                self.conn.register(name, arrow)
                try:
                    self.conn.execute(f"INSERT INTO {table} SELECT * FROM {name}")
                finally:
                    self.conn.unregister(name)
            return
        # 80 rows keeps SQLite's variable count comfortably below conservative
        # builds while avoiding DuckDB's per-row executemany overhead.
        for start in range(0, len(rows), 80):
            group = rows[start : start + 80]
            placeholders = ", ".join(
                "(" + ", ".join("?" for _ in range(width)) + ")" for _ in group
            )
            self.conn.execute(
                f"INSERT INTO {table} VALUES {placeholders}",
                [value for row in group for value in row],
            )

    @staticmethod
    def _document_payload(document: Document) -> dict:
        return {**asdict(document), "asset": hashlib.sha256(document.asset).hexdigest()}

    def import_documents(
        self, operation_id: str, documents: Iterable[Document], *, batch_size: int = 500
    ) -> str:
        # Staging is keyed by operation ID.  Keep imports distinct without
        # blocking ordinary UI reads between their durable batch commits.
        with self._import_lock:
            return self._import_documents(
                operation_id, documents, batch_size=batch_size
            )

    def _import_documents(
        self, operation_id: str, documents: Iterable[Document], *, batch_size: int
    ) -> str:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        # A completed retry only needs a streaming digest; it does not stage data.
        existing = self._one(
            "SELECT * FROM receipts WHERE operation_id=?", (operation_id,)
        )
        if existing:
            digest = hashlib.sha256()
            for document in documents:
                digest.update(_canonical(self._document_payload(document)))
            return self._receipt(operation_id, "import", digest.hexdigest()) or ""
        hasher = hashlib.sha256()
        with self._tx():
            self.conn.execute(
                "DELETE FROM staged_documents WHERE operation_id=?", (operation_id,)
            )
        batch: list[tuple] = []
        ordinal = 0
        for document in documents:
            if not isinstance(document, Document):
                raise TypeError("documents must contain Document values")
            if (
                type(document.row_id) is not int
                or not all(
                    isinstance(field, str)
                    for field in (document.title, document.category, document.text)
                )
                or (
                    document.raw_amount is not None
                    and not isinstance(document.raw_amount, str)
                )
            ):
                raise TypeError("document fields have invalid types")
            hasher.update(_canonical(self._document_payload(document)))
            parsed, state = _parsed_amount(document.raw_amount)
            asset_hash = self._asset(document.asset)
            source = _identifier(
                "source",
                operation_id,
                document.row_id,
                _digest(
                    [
                        document.title,
                        document.category,
                        document.raw_amount,
                        document.text,
                        asset_hash,
                    ]
                ),
            )
            batch.append(
                (
                    operation_id,
                    ordinal,
                    document.row_id,
                    document.title,
                    document.category,
                    document.raw_amount,
                    parsed,
                    state,
                    document.text,
                    asset_hash,
                    source,
                )
            )
            ordinal += 1
            if len(batch) >= batch_size:
                self._stage_batch(batch)
                batch.clear()
        if batch:
            self._stage_batch(batch)
        digest = hasher.hexdigest()
        with self._tx("import.before_publish"):
            # Check again inside publication to make a concurrent retry harmless.
            receipt = self._receipt(operation_id, "import", digest)
            if receipt:
                return receipt
            duplicate = self._one(
                "SELECT COUNT(*) AS n FROM (SELECT row_id FROM staged_documents WHERE operation_id=? GROUP BY row_id HAVING COUNT(*) > 1)",
                (operation_id,),
            )
            if duplicate["n"]:
                raise ValueError("row_id values must be unique within an import")
            existing_rows = self._one(
                "SELECT COUNT(*) AS n FROM staged_documents s JOIN documents d ON d.row_id=s.row_id WHERE s.operation_id=?",
                (operation_id,),
            )
            if existing_rows["n"]:
                raise Conflict("import row_id already exists")
            self.conn.execute(
                """INSERT INTO sources (source_version, row_id, title, category, raw_amount, text, asset_hash)
                              SELECT source_version, row_id, title, category, raw_amount, text, asset_hash
                              FROM staged_documents WHERE operation_id=?""",
                (operation_id,),
            )
            self.conn.execute(
                """INSERT INTO documents (row_id, title, category, raw_amount, parsed_amount, amount_state, source_version, asset_hash)
                              SELECT row_id, title, category, raw_amount, parsed_amount, amount_state, source_version, asset_hash
                              FROM staged_documents WHERE operation_id=?""",
                (operation_id,),
            )
            self.conn.execute(
                "DELETE FROM staged_documents WHERE operation_id=?", (operation_id,)
            )
            return self._insert_receipt(operation_id, "import", digest)

    def _stage_batch(self, batch: list[tuple]) -> None:
        with self._tx():
            self._insert_many("staged_documents", 11, batch)
        self._checkpoint("import.batch_committed")

    def discard_staged(self, operation_id: str) -> None:
        with self._import_lock:
            with self._tx():
                self.conn.execute(
                    "DELETE FROM staged_documents WHERE operation_id=?", (operation_id,)
                )

    @staticmethod
    def _output_payload(outputs: Sequence[Extraction]) -> list[dict]:
        return [asdict(output) for output in outputs]

    def publish_extraction(
        self, operation_id: str, outputs: Sequence[Extraction]
    ) -> str:
        if any(not _valid_cents(output.amount_cents) for output in outputs):
            raise ValueError(
                "amount_cents must be an int in signed 64-bit range or None"
            )
        digest = _digest(self._output_payload(outputs))
        with self._tx("extract.before_commit"):
            receipt = self._receipt(operation_id, "extract", digest)
            if receipt:
                return receipt
            for start in range(0, len(outputs), 500):
                batch = outputs[start : start + 500]
                row_ids = list(dict.fromkeys(output.row_id for output in batch))
                placeholders = ", ".join("?" for _ in row_ids)
                documents = {
                    row["row_id"]: row
                    for row in self._rows(
                        "SELECT d.row_id, d.source_version, s.text FROM documents d "
                        "JOIN sources s ON s.source_version=d.source_version "
                        f"WHERE d.row_id IN ({placeholders})",
                        row_ids,
                    )
                }
                result_rows, head_rows, citation_rows = [], [], []
                for offset, output in enumerate(batch):
                    document = documents.get(output.row_id)
                    if (
                        not document
                        or document["source_version"] != output.source_version
                    ):
                        raise Conflict("extraction source_version is no longer current")
                    matches: list[tuple[str, int, int]] = []
                    for quote in output.quotes:
                        found = quote_ranges(document["text"], quote)
                        if not found:
                            raise InvalidCitation(
                                f"quote does not align for row {output.row_id}"
                            )
                        matches.extend(
                            (quote, quote_start, end) for quote_start, end in found
                        )
                    version = _identifier(
                        "result", operation_id, start + offset, _digest(asdict(output))
                    )
                    result_rows.append(
                        (
                            version,
                            output.row_id,
                            output.source_version,
                            output.amount_cents,
                            "generated",
                        )
                    )
                    head_rows.append((output.row_id, version))
                    for quote_index, (quote, quote_start, end) in enumerate(matches):
                        citation = _identifier(
                            "citation", version, quote_index, quote_start, end
                        )
                        citation_rows.append(
                            (
                                citation,
                                output.row_id,
                                version,
                                output.source_version,
                                quote_start,
                                end,
                                quote,
                            )
                        )
                self._insert_many("results", 5, result_rows)
                if head_rows:
                    self.conn.execute(
                        "DELETE FROM generated_heads WHERE row_id IN ("
                        + ", ".join("?" for _ in head_rows)
                        + ")",
                        [row[0] for row in head_rows],
                    )
                    self._insert_many("generated_heads", 2, head_rows)
                self._insert_many("citations", 7, citation_rows)
            return self._insert_receipt(operation_id, "extract", digest)

    def _effective_sql(self) -> str:
        return """
            SELECT d.row_id, d.title, d.category, d.raw_amount, d.amount_state, d.source_version,
                   CASE WHEN mh.row_id IS NOT NULL THEN mr.version_id ELSE gr.version_id END AS current_version,
                   CASE WHEN mh.row_id IS NOT NULL THEN mr.amount_cents ELSE gr.amount_cents END AS amount_cents,
                   CASE WHEN rs.version_id=CASE WHEN mh.row_id IS NOT NULL THEN mr.version_id ELSE gr.version_id END THEN rs.decision
                        ELSE 'unreviewed' END AS review_state
            FROM documents d
            LEFT JOIN manual_heads mh ON mh.row_id=d.row_id
            LEFT JOIN results mr ON mr.version_id=mh.version_id
            LEFT JOIN generated_heads gh ON gh.row_id=d.row_id
            LEFT JOIN results gr ON gr.version_id=gh.version_id
            LEFT JOIN review_status rs ON rs.row_id=d.row_id
        """

    def read_rows(
        self,
        *,
        category: str | None = None,
        limit: int = 50,
        after_row_id: int = 0,
        sort: str = "row_id",
    ) -> list[dict]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if sort not in {"row_id", "amount_cents"}:
            raise ValueError("sort must be row_id or amount_cents")
        clauses, params = [], []
        if category is not None:
            clauses.append("category=?")
            params.append(category)
        if sort == "row_id":
            clauses.append("row_id>?")
            params.append(after_row_id)
            order = "row_id"
        else:
            order = "amount_cents IS NULL, amount_cents, row_id"
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return self._rows(
            f"SELECT * FROM ({self._effective_sql()}) visible{where} ORDER BY {order} LIMIT ?",
            (*params, limit),
        )

    def review_value(
        self,
        operation_id: str,
        row_id: int,
        decision: str,
        *,
        expected_version: str,
        value: int | None = None,
    ) -> str:
        if decision not in {"accept", "reject", "edit"}:
            raise ValueError("decision must be accept, reject, or edit")
        if decision == "edit" and value is None:
            raise ValueError("edit requires value")
        if decision != "edit" and value is not None:
            raise ValueError("only edit accepts value")
        if not _valid_cents(value):
            raise ValueError("value must be an int in signed 64-bit range or None")
        payload = {
            "row_id": row_id,
            "decision": decision,
            "expected_version": expected_version,
            "value": value,
        }
        digest = _digest(payload)
        with self._tx("review.before_commit"):
            receipt = self._receipt(operation_id, "review", digest)
            if receipt:
                return receipt
            current = self._one(
                f"SELECT current_version FROM ({self._effective_sql()}) current WHERE row_id=?",
                (row_id,),
            )
            if not current or current["current_version"] != expected_version:
                raise Conflict("expected_version is stale")
            if decision == "accept":
                self.conn.execute(
                    "INSERT OR REPLACE INTO review_status VALUES (?, ?, ?)",
                    (row_id, "accepted", expected_version),
                )
            else:
                source = self._one(
                    "SELECT source_version FROM documents WHERE row_id=?", (row_id,)
                )
                version = _identifier("manual", operation_id, row_id, value)
                self.conn.execute(
                    "INSERT INTO results VALUES (?, ?, ?, ?, ?)",
                    (version, row_id, source["source_version"], value, "manual"),
                )
                self.conn.execute(
                    "INSERT OR REPLACE INTO manual_heads VALUES (?, ?)",
                    (row_id, version),
                )
                self.conn.execute(
                    "INSERT OR REPLACE INTO review_status VALUES (?, ?, ?)",
                    (row_id, "edited" if decision == "edit" else "rejected", version),
                )
            return self._insert_receipt(operation_id, "review", digest)

    def replace_text(self, operation_id: str, row_id: int, text: str) -> str:
        payload = {"row_id": row_id, "text": text}
        digest = _digest(payload)
        with self._tx():
            receipt = self._receipt(operation_id, "replace_text", digest)
            if receipt:
                return receipt
            document = self._one("SELECT * FROM documents WHERE row_id=?", (row_id,))
            if not document:
                raise KeyError(row_id)
            source = _identifier(
                "source",
                operation_id,
                row_id,
                _digest([document["title"], text, document["asset_hash"]]),
            )
            self.conn.execute(
                "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    source,
                    row_id,
                    document["title"],
                    document["category"],
                    document["raw_amount"],
                    text,
                    document["asset_hash"],
                ),
            )
            self.conn.execute(
                "UPDATE documents SET source_version=? WHERE row_id=?", (source, row_id)
            )
            return self._insert_receipt(operation_id, "replace_text", digest)

    def citations(self, row_id: int) -> list[dict]:
        return self._rows(
            "SELECT c.citation_id, c.source_version, c.start_offset AS start, c.end_offset AS end, c.quote FROM citations c JOIN generated_heads h ON h.version_id=c.version_id WHERE c.row_id=? ORDER BY c.start_offset, c.citation_id",
            (row_id,),
        )

    def resolve_citation(self, citation_id: str) -> dict:
        citation = self._one(
            "SELECT * FROM citations WHERE citation_id=?", (citation_id,)
        )
        if not citation:
            raise KeyError(citation_id)
        source = self._one(
            "SELECT text FROM sources WHERE source_version=?",
            (citation["source_version"],),
        )
        document = self._one(
            "SELECT source_version FROM documents WHERE row_id=?", (citation["row_id"],)
        )
        manual = self._one(
            "SELECT 1 AS present FROM manual_heads WHERE row_id=?",
            (citation["row_id"],),
        )
        generated = self._one(
            "SELECT version_id FROM generated_heads WHERE row_id=?",
            (citation["row_id"],),
        )
        return {
            "citation_id": citation_id,
            "source_version": citation["source_version"],
            "start": citation["start_offset"],
            "end": citation["end_offset"],
            "quote": citation["quote"],
            "text": source["text"],
            "stale": bool(
                manual
                or not document
                or document["source_version"] != citation["source_version"]
                or not generated
                or generated["version_id"] != citation["version_id"]
            ),
        }

    def aggregate(self, *, category: str | None = None) -> list[dict]:
        filter_sql, params = (
            ("WHERE category=?", [category]) if category is not None else ("", [])
        )
        sql = f"""
            WITH visible AS ({self._effective_sql()}), filtered AS (SELECT * FROM visible {filter_sql}),
            grouped AS (SELECT category, COUNT(*) AS row_count, COUNT(amount_cents) AS value_count,
                        SUM(amount_cents) AS sum_cents, AVG(amount_cents) AS mean_cents FROM filtered GROUP BY category),
            ranked AS (SELECT category, amount_cents, ROW_NUMBER() OVER (PARTITION BY category ORDER BY amount_cents) AS rn,
                       COUNT(*) OVER (PARTITION BY category) AS n FROM filtered WHERE amount_cents IS NOT NULL),
            medians AS (SELECT category, AVG(amount_cents) AS median_cents FROM ranked WHERE rn BETWEEN n / 2.0 AND n / 2.0 + 1 GROUP BY category)
            SELECT g.category, g.row_count, g.value_count, g.sum_cents, g.mean_cents, m.median_cents
            FROM grouped g LEFT JOIN medians m ON m.category=g.category ORDER BY g.category
        """
        return self._rows(sql, params)

    def history_join(self, *, row_id_start: int, row_id_end: int) -> list[dict]:
        """Summarize retained result history for one selective row range."""

        if row_id_start < 1 or row_id_end < row_id_start:
            raise ValueError("history range is invalid")
        return self._rows(
            "SELECT d.category,COUNT(*) AS version_count,"
            "COUNT(DISTINCT r.row_id) AS row_count,COUNT(r.amount_cents) AS value_count "
            "FROM documents d JOIN results r ON r.row_id=d.row_id "
            "WHERE d.row_id BETWEEN ? AND ? GROUP BY d.category ORDER BY d.category",
            (row_id_start, row_id_end),
        )

    def checkpoint_storage(self) -> object:
        """Fold engine write-ahead state into the durable project file."""

        if self.engine == "sqlite":
            return self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return self.conn.execute("CHECKPOINT").fetchone()

    def logical_counts(self) -> dict[str, int]:
        queries = {
            "visible_rows": "SELECT COUNT(*) AS n FROM documents",
            "result_versions": "SELECT COUNT(*) AS n FROM results",
            "citations": "SELECT COUNT(*) AS n FROM citations",
            "receipts": "SELECT COUNT(*) AS n FROM receipts",
            "staged_rows": "SELECT COUNT(*) AS n FROM staged_documents",
        }
        return {name: int(self._one(query)["n"]) for name, query in queries.items()}

    def export_project(self, destination: Path) -> Path:
        with self._snapshot():
            return self._export_project(destination)

    def _export_project(self, destination: Path) -> Path:
        destination = Path(destination)
        if destination.exists():
            raise FileExistsError(destination)
        if self._one("SELECT COUNT(*) AS n FROM staged_documents")["n"]:
            raise RuntimeError("cannot export while an import remains staged")
        temporary = destination.with_name(
            destination.name + ".export-stage-" + uuid.uuid4().hex
        )
        temporary.mkdir(parents=True)
        try:
            manifest = {
                "format": "storage-slice-logical-v1",
                "tables": {},
                "assets": {},
            }
            for table in _TABLES:
                count, hasher = 0, hashlib.sha256()
                with (temporary / f"{table}.jsonl").open("wb") as out:
                    cursor = self.conn.execute(f"SELECT * FROM {table}")
                    names = [part[0] for part in cursor.description]
                    if tuple(names) != _TABLE_COLUMNS[table]:
                        raise RuntimeError("unexpected fixed schema")
                    while rows := cursor.fetchmany(256):
                        for values in rows:
                            payload = (
                                json.dumps(
                                    dict(zip(names, values)),
                                    sort_keys=True,
                                    ensure_ascii=False,
                                )
                                + "\n"
                            ).encode()
                            out.write(payload)
                            hasher.update(payload)
                            count += 1
                manifest["tables"][table] = {
                    "count": count,
                    "sha256": hasher.hexdigest(),
                }
            target_assets = temporary / "assets"
            target_assets.mkdir()
            asset_count, asset_hasher = 0, hashlib.sha256()
            cursor = self.conn.execute(
                "SELECT DISTINCT asset_hash FROM sources ORDER BY asset_hash"
            )
            while rows := cursor.fetchmany(256):
                for (asset_hash,) in rows:
                    source, target = (
                        self.path / "assets" / asset_hash,
                        target_assets / asset_hash,
                    )
                    if not source.exists() or _file_digest(source) != asset_hash:
                        raise RuntimeError("referenced asset is missing or corrupt")
                    with source.open("rb") as inp, target.open("wb") as out:
                        shutil.copyfileobj(inp, out, length=64 * 1024)
                    asset_hasher.update((asset_hash + "\n").encode())
                    asset_count += 1
            manifest["assets"] = {
                "count": asset_count,
                "sha256": asset_hasher.hexdigest(),
            }
            (temporary / "manifest.json").write_text(
                json.dumps(manifest, sort_keys=True), encoding="utf-8"
            )
            temporary.replace(destination)
            return destination
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    @classmethod
    def restore_project(
        cls, export_path: Path, path: Path, engine: str
    ) -> "ProjectStore":
        export_path, path = Path(export_path), Path(path)
        if path.exists():
            raise FileExistsError(path)
        manifest = json.loads(
            (export_path / "manifest.json").read_text(encoding="utf-8")
        )
        table_manifest = manifest.get("tables")
        if (
            manifest.get("format") != "storage-slice-logical-v1"
            or not isinstance(table_manifest, dict)
            or set(table_manifest) != set(_TABLES)
        ):
            raise ValueError("unsupported logical export")
        asset_manifest = manifest.get("assets")
        if not isinstance(asset_manifest, dict):
            raise ValueError("invalid asset manifest")
        stage = path.with_name(path.name + ".restore-stage-" + uuid.uuid4().hex)
        try:
            store = cls(stage, engine)
            with store._tx():
                for table in _TABLES:
                    source = export_path / f"{table}.jsonl"
                    if not source.is_file():
                        raise ValueError(f"missing table {table}")
                    count, hasher, batch = 0, hashlib.sha256(), []
                    with source.open("rb") as lines:
                        for line in lines:
                            hasher.update(line)
                            record = json.loads(line)
                            if set(record) != set(_TABLE_COLUMNS[table]):
                                raise ValueError(f"invalid columns for {table}")
                            batch.append(
                                tuple(
                                    record[column] for column in _TABLE_COLUMNS[table]
                                )
                            )
                            count += 1
                            if len(batch) >= 80:
                                store._insert_many(
                                    table, len(_TABLE_COLUMNS[table]), batch
                                )
                                batch.clear()
                    if batch:
                        store._insert_many(table, len(_TABLE_COLUMNS[table]), batch)
                    expected = table_manifest[table]
                    if (
                        not isinstance(expected, dict)
                        or expected.get("count") != count
                        or expected.get("sha256") != hasher.hexdigest()
                    ):
                        raise ValueError(f"table verification failed for {table}")
                asset_count, asset_hasher = 0, hashlib.sha256()
                cursor = store.conn.execute(
                    "SELECT DISTINCT asset_hash FROM sources ORDER BY asset_hash"
                )
                while rows := cursor.fetchmany(256):
                    for (asset_hash,) in rows:
                        source = export_path / "assets" / asset_hash
                        if not source.is_file() or _file_digest(source) != asset_hash:
                            raise ValueError("asset hash verification failed")
                        with (
                            source.open("rb") as inp,
                            (stage / "assets" / asset_hash).open("wb") as out,
                        ):
                            shutil.copyfileobj(inp, out, length=64 * 1024)
                        asset_hasher.update((asset_hash + "\n").encode())
                        asset_count += 1
                if (
                    asset_manifest.get("count") != asset_count
                    or asset_manifest.get("sha256") != asset_hasher.hexdigest()
                ):
                    raise ValueError("asset manifest does not match referenced assets")
            store.close()
            stage.replace(path)
            return cls(path, engine)
        except BaseException:
            if "store" in locals():
                store.close()
            shutil.rmtree(stage, ignore_errors=True)
            raise
