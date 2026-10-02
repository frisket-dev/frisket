"""Measure Frisket's search sidecar and bounded FTS5 layout alternatives.

This is an opt-in research probe.  It imports a repository-owned corpus through
the production streaming writer, builds the production search index, and then
rebuilds the same indexed rows into disposable candidate databases.  It does
not modify production schema or data.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import resource
import shutil
import sqlite3
import time
import zlib

from frisket.engine.store import Project
from frisket.engine.store.streaming_import import StreamingSheetWriter
from frisket.sample_content.lawsuits import SAMPLE_LAWSUITS
from frisket.search import search_cells_scoped, search_project_page, search_sheet
from frisket.search_index import index_batch
from scripts.storage_slice.fixtures import (
    stress_project_records,
    stress_project_search_tokens,
)


MIB = 1024**2
MAX_ROWS = 10_000
MAX_SCRATCH = 500 * MIB
MAX_ADDRESS_SPACE = 1024 * MIB
PAGE_ROWS = 500
COLUMNS = [
    {"name": "record_id", "type": "integer"},
    {"name": "title", "type": "text", "format": "plain_text"},
    {"name": "body", "type": "text", "format": "plain_text"},
    {"name": "category", "type": "category"},
    {"name": "amount", "type": "integer"},
    {"name": "published_at", "type": "date"},
    {"name": "status", "type": "category"},
]
QUERIES = ('"recorded request"', "frisketlate00000199", "frisketlate0000019*")


def _directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _guard(root: Path) -> None:
    used = _directory_bytes(root)
    if used > MAX_SCRATCH:
        raise RuntimeError(f"scratch limit exceeded: {used} > {MAX_SCRATCH}")


def _dbstat(db: sqlite3.Connection) -> list[dict[str, int | str]]:
    return [
        {
            "name": str(row[0]),
            "pages": int(row[1]),
            "bytes": int(row[2]),
            "payload_bytes": int(row[3]),
            "unused_bytes": int(row[4]),
        }
        for row in db.execute(
            "SELECT name,COUNT(*),SUM(pgsize),SUM(payload),SUM(unused) "
            "FROM dbstat GROUP BY name ORDER BY SUM(pgsize) DESC,name"
        )
    ]


def _layout(path: Path, *, logical_sql: str | None = None) -> dict:
    db = sqlite3.connect(path)
    try:
        page_size = int(db.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(db.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(db.execute("PRAGMA freelist_count").fetchone()[0])
        result = {
            "path": str(path),
            "file_bytes": path.stat().st_size,
            "page_size": page_size,
            "page_count": page_count,
            "freelist_count": freelist_count,
            "allocated_bytes": page_size * page_count,
            "dbstat": _dbstat(db),
        }
        if logical_sql:
            cursor = db.execute(logical_sql)
            row = cursor.fetchone()
            result["logical"] = {
                key: int(value or 0)
                for key, value in zip((item[0] for item in cursor.description), row)
            }
        return result
    finally:
        db.close()


def _source_rows(source: sqlite3.Connection):
    yield from source.execute(
        "SELECT f.rowid,f.content,f.sheet_id,f.row_id,f.column_id,f.column_name,"
        "s.source_hash FROM cell_fts AS f JOIN search_cells AS s ON s.id=f.rowid "
        "ORDER BY f.rowid"
    )


def _candidate_schema(db: sqlite3.Connection, kind: str) -> None:
    db.executescript(
        "CREATE TABLE search_cells("
        "id INTEGER PRIMARY KEY,sheet_id INTEGER NOT NULL,column_id INTEGER NOT NULL,"
        "row_id INTEGER NOT NULL,source_hash BLOB NOT NULL,"
        "UNIQUE(column_id,row_id));"
        "CREATE INDEX search_cells_sheet_column "
        "ON search_cells(sheet_id,column_id,row_id);"
    )
    if kind == "compact_contentful":
        db.execute("CREATE VIRTUAL TABLE cell_fts USING fts5(content)")
    elif kind == "compressed_external":
        db.create_function(
            "inflate_utf8",
            1,
            lambda value: zlib.decompress(value).decode("utf-8"),
            deterministic=True,
        )
        db.executescript(
            "CREATE TABLE search_content("
            "id INTEGER PRIMARY KEY,content_zlib BLOB NOT NULL);"
            "CREATE VIEW search_content_view AS "
            "SELECT id,inflate_utf8(content_zlib) AS content FROM search_content;"
            "CREATE VIRTUAL TABLE cell_fts USING fts5("
            "content,content='search_content_view',content_rowid='id');"
        )
    elif kind == "contentless":
        db.execute(
            "CREATE VIRTUAL TABLE cell_fts USING fts5("
            "content,content='',contentless_delete=1)"
        )
    else:
        raise ValueError(kind)


def _query_signature(db: sqlite3.Connection, query: str, *, snippets: bool) -> list:
    snippet = "snippet(cell_fts,0,'<b>','</b>','…',12)" if snippets else "NULL"
    return [
        [int(row[0]), round(float(row[1]), 9), row[2]]
        for row in db.execute(
            "SELECT cell_fts.rowid,rank," + snippet + " FROM cell_fts "
            "WHERE cell_fts MATCH ? ORDER BY rank,cell_fts.rowid LIMIT 20",
            (query,),
        )
    ]


def _highlight_signature(
    db: sqlite3.Connection, query: str, *, content_available: bool
) -> dict | None:
    row = db.execute(
        "SELECT highlight(cell_fts,0,'<b>','</b>') FROM cell_fts "
        "WHERE cell_fts MATCH ? ORDER BY rank,cell_fts.rowid LIMIT 1",
        (query,),
    ).fetchone()
    if not content_available:
        assert row == (None,)
        return None
    value = str(row[0])
    assert "<b>" in value and "</b>" in value
    return {
        "utf8_bytes": len(value.encode()),
        "sha256": hashlib.sha256(value.encode()).hexdigest(),
    }


def _compression_sample(values: list[str]) -> dict[str, int | float]:
    logical = sum(len(value.encode()) for value in values)
    compressed = sum(len(zlib.compress(value.encode(), level=6)) for value in values)
    return {
        "values": len(values),
        "logical_bytes": logical,
        "compressed_bytes": compressed,
        "compressed_fraction": compressed / logical,
    }


def _compression_sensitivity() -> dict[str, dict[str, int | float]]:
    natural = ["\n\n".join(lawsuit.pages) for lawsuit in SAMPLE_LAWSUITS]
    less_repetitive = [
        base64.b85encode(hashlib.shake_256(str(index).encode()).digest(3_072)).decode()
        for index in range(256)
    ]
    return {
        "repository_lawsuit_text": _compression_sample(natural),
        "deterministic_less_repetitive_text": _compression_sample(less_repetitive),
    }


def _build_candidate(
    source_path: Path, candidate_path: Path, kind: str, root: Path
) -> dict:
    candidate_path.unlink(missing_ok=True)
    source = sqlite3.connect(source_path)
    candidate = sqlite3.connect(candidate_path)
    started = time.perf_counter()
    try:
        _candidate_schema(candidate, kind)
        candidate.execute("BEGIN")
        logical_content = compressed_content = 0
        inserted = 0
        for indexed_id, content, sheet, row, column, _name, source_hash in _source_rows(
            source
        ):
            text = str(content)
            digest = bytes.fromhex(str(source_hash))
            candidate.execute(
                "INSERT INTO search_cells(id,sheet_id,column_id,row_id,source_hash) "
                "VALUES (?,?,?,?,?)",
                (indexed_id, sheet, column, row, digest),
            )
            if kind == "compressed_external":
                packed = zlib.compress(text.encode("utf-8"), level=6)
                candidate.execute(
                    "INSERT INTO search_content(id,content_zlib) VALUES (?,?)",
                    (indexed_id, packed),
                )
                compressed_content += len(packed)
            candidate.execute(
                "INSERT INTO cell_fts(rowid,content) VALUES (?,?)", (indexed_id, text)
            )
            logical_content += len(text.encode("utf-8"))
            inserted += 1
            if inserted % 5_000 == 0:
                _guard(root)
        candidate.commit()
        snippets = kind != "contentless"
        signatures = {
            query: _query_signature(candidate, query, snippets=snippets)
            for query in QUERIES
        }
        highlight = _highlight_signature(
            candidate, "frisketlate00000199", content_available=snippets
        )
        if kind == "compressed_external":
            # Reopen and re-register the connection-local codec.  This is the
            # operational seam production readers would also have to own.
            candidate.close()
            candidate = sqlite3.connect(candidate_path)
            candidate.create_function(
                "inflate_utf8",
                1,
                lambda value: zlib.decompress(value).decode("utf-8"),
                deterministic=True,
            )
            reopened = {
                query: _query_signature(candidate, query, snippets=True)
                for query in QUERIES
            }
            assert reopened == signatures
        result = _layout(candidate_path)
        result.update(
            {
                "kind": kind,
                "build_seconds": time.perf_counter() - started,
                "indexed_rows": inserted,
                "logical_content_bytes": logical_content,
                "compressed_content_bytes": compressed_content,
                "query_signatures": signatures,
                "highlight_signature": highlight,
            }
        )
        return result
    finally:
        source.close()
        candidate.close()


def _build_project(bundle: Path, rows: int, root: Path) -> tuple[dict, int, dict]:
    if bundle.exists():
        shutil.rmtree(bundle)
    project = Project.create(bundle, name="search size probe")
    writer = StreamingSheetWriter.start_session(
        project,
        session_id="search-size-import",
        writer_authority="search-size-probe",
        sheet_name="Investigative records",
        columns=COLUMNS,
        project_id="search-size-probe",
        action_kind="import.files",
        idempotency_key="search-size-probe-v1",
        params_hash="sha256:search-size-probe-v1",
        action_id="act:search-size-probe-v1",
        receipt_id="receipt:search-size-probe-v1",
        source_ref={"kind": "generated_fixture", "rows": rows},
    )
    cursor = 0
    body_bytes = input_bytes = 0
    sample_row_id = None
    for start in range(1, rows + 1, PAGE_ROWS):
        page = list(stress_project_records(start, min(PAGE_ROWS, rows - start + 1)))
        encoded = [
            json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode()
            + b"\n"
            for record in page
        ]
        ids = writer.append_page(
            page,
            expected_cursor=cursor,
            next_cursor=cursor + len(page),
            committed_bytes=sum(map(len, encoded)),
        )
        if start <= 199 < start + len(page):
            sample_row_id = ids[199 - start]
        cursor += len(page)
        input_bytes += sum(map(len, encoded))
        body_bytes += sum(len(str(record["body"]).encode()) for record in page)
        _guard(root)
    publication = writer.finalize_session(expected_cursor=cursor)
    columns = {
        str(c["name"]): int(c["id"]) for c in project.columns(publication.sheet_id)
    }
    while True:
        progress = index_batch(project, batch_size=5_000, max_bytes=8 * MIB)
        _guard(root)
        if progress.complete:
            break
    assert sample_row_id is not None
    tokens = stress_project_search_tokens(199)
    page = search_project_page(project, tokens["late"], limit=10, rerank="off")
    assert page["complete"] and [hit["row_id"] for hit in page["hits"]] == [
        sample_row_id
    ]
    assert tokens["late"] in page["hits"][0]["snip"]
    assert search_sheet(project, publication.sheet_id, tokens["late"]) == [
        sample_row_id
    ]
    scoped = search_cells_scoped(
        project,
        publication.sheet_id,
        tokens["late"],
        [],
        {(sample_row_id, columns["body"])},
        limit=10,
    )
    assert len(scoped) == 1 and tokens["late"] in scoped[0]["snip"]
    sidecar = sqlite3.connect(bundle / "project.search.db")
    baseline_signatures = {
        query: _query_signature(sidecar, query, snippets=True) for query in QUERIES
    }
    baseline_highlight = _highlight_signature(
        sidecar, "frisketlate00000199", content_available=True
    )
    sidecar.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    sidecar.close()
    project.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    project.close()
    return (
        {
            "rows": rows,
            "input_ndjson_bytes": input_bytes,
            "body_utf8_bytes": body_bytes,
            "searchable_cells": rows * 4,
        },
        publication.sheet_id,
        {
            "baseline_query_signatures": baseline_signatures,
            "baseline_highlight_signature": baseline_highlight,
        },
    )


def _optimized_copy(source_path: Path, output_path: Path) -> dict:
    output_path.unlink(missing_ok=True)
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(output_path)
    try:
        source.backup(target)
        before = _layout(output_path)
        started = time.perf_counter()
        target.execute("INSERT INTO cell_fts(cell_fts) VALUES('optimize')")
        target.commit()
        after_optimize = _layout(output_path)
        target.execute("VACUUM")
        after_vacuum = _layout(output_path)
        return {
            "seconds": time.perf_counter() - started,
            "before": before,
            "after_optimize": after_optimize,
            "after_vacuum": after_vacuum,
        }
    finally:
        source.close()
        target.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=10_000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 200 <= args.rows <= MAX_ROWS:
        raise SystemExit(f"--rows must be between 200 and {MAX_ROWS}")
    resource.setrlimit(resource.RLIMIT_AS, (MAX_ADDRESS_SPACE, MAX_ADDRESS_SPACE))
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    bundle = root / f"fixture-{args.rows}.frisket"
    fixture, sheet_id, validation = _build_project(bundle, args.rows, root)
    sidecar_path = bundle / "project.search.db"
    project_path = bundle / "project.db"
    baseline = _layout(
        sidecar_path,
        logical_sql=(
            "SELECT COUNT(*) AS rows,SUM(length(c0)) AS content_bytes,"
            "SUM(length(c1)) AS sheet_id_bytes,SUM(length(c2)) AS row_id_bytes,"
            "SUM(length(c3)) AS column_id_bytes,SUM(length(c4)) AS column_name_bytes "
            "FROM cell_fts_content"
        ),
    )
    source = sqlite3.connect(sidecar_path)
    baseline["logical"]["source_hash_text_bytes"] = int(
        source.execute("SELECT SUM(length(source_hash)) FROM search_cells").fetchone()[
            0
        ]
    )
    source.close()
    candidates = {}
    for kind in ("compact_contentful", "compressed_external", "contentless"):
        candidate_path = root / f"candidate-{kind}.db"
        candidates[kind] = _build_candidate(sidecar_path, candidate_path, kind, root)
        candidate_path.unlink()
    for kind in ("compact_contentful", "compressed_external"):
        candidate = candidates[kind]
        candidate["native_query_equivalent"] = (
            candidate["query_signatures"] == validation["baseline_query_signatures"]
            and candidate["highlight_signature"]
            == validation["baseline_highlight_signature"]
        )
    candidates["contentless"]["ranked_match_equivalent"] = all(
        [row[:2] for row in candidates["contentless"]["query_signatures"][query]]
        == [row[:2] for row in validation["baseline_query_signatures"][query]]
        for query in QUERIES
    )
    optimized_path = root / "candidate-optimized.db"
    optimized = _optimized_copy(sidecar_path, optimized_path)
    optimized_path.unlink()
    report = {
        "schema": "frisket.search-size-probe.v1",
        "fixture": fixture,
        "sheet_id": sheet_id,
        "validation": validation,
        "project": _layout(project_path),
        "baseline_search": baseline,
        "candidates": candidates,
        "optimized": optimized,
        "compression_sensitivity": _compression_sensitivity(),
        "scratch_bytes_retained": _directory_bytes(root),
        "limits": {
            "max_rows": MAX_ROWS,
            "max_scratch_bytes": MAX_SCRATCH,
            "max_address_space_bytes": MAX_ADDRESS_SPACE,
        },
    }
    report_path = root / "search-size-probe.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(report_path)


if __name__ == "__main__":
    main()
