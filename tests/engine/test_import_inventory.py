from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from frisket.engine.store.import_inventory import ImportInventory
from frisket.engine.store.import_intake import (
    append_inventory_batch,
    inventory_batch_through,
)


def _item(index: int, *, size: int = 10, digest: str | None = None) -> dict:
    digest = digest or hashlib.sha256(f"bytes-{index}".encode()).hexdigest()
    return {
        "logical_path": f"folder/item-{index}.eml",
        "filename": "ignored-name",
        "mime": "message/rfc822",
        "sha256": digest,
        "size": size,
        "kind": "email",
        "email_format": "eml",
        "path": "ignored/path",
    }


def test_reopen_preserves_occurrences_summaries_and_derived_fields(tmp_path: Path):
    path = tmp_path / "plan" / "inventory.sqlite3"
    path.parent.mkdir()
    digest = hashlib.sha256(b"identical").hexdigest()
    with ImportInventory(path) as inventory:
        assert inventory.append(
            (_item(index, digest=digest) for index in range(2))
        ) == (
            1,
            2,
        )
        assert inventory.totals() == {"count": 2, "bytes": 20}

    with ImportInventory(path) as reopened:
        rows = reopened.page(limit=10)
        assert [row["ordinal"] for row in rows] == [1, 2]
        assert [row["logical_path"] for row in rows] == [
            "folder/item-0.eml",
            "folder/item-1.eml",
        ]
        assert rows[0]["sha256"] == rows[1]["sha256"] == digest
        assert rows[0]["filename"] == "item-0.eml"
        assert rows[0]["path"] == f"files/{digest}"


def test_keyset_pages_honor_count_and_byte_budget_with_oversized_first(tmp_path: Path):
    with ImportInventory(tmp_path / "inventory.sqlite3") as inventory:
        inventory.append(
            [_item(0, size=12), _item(1, size=4), _item(2, size=5), _item(3, size=1)]
        )
        first = inventory.page(limit=3, byte_budget=10)
        assert [row["ordinal"] for row in first] == [1]
        second = inventory.page(after=first[-1]["ordinal"], limit=3, byte_budget=10)
        assert [row["ordinal"] for row in second] == [2, 3, 4]
        assert inventory.page(after=4, limit=3, byte_budget=10) == []


def test_seal_refuses_appends_but_allows_pages_and_survives_reopen(tmp_path: Path):
    path = tmp_path / "inventory.sqlite3"
    with ImportInventory(path) as inventory:
        inventory.append([_item(0)])
        inventory.seal()
        inventory.seal()
        assert inventory.sealed
        with pytest.raises(RuntimeError, match="sealed"):
            inventory.append([_item(1)])
        assert len(inventory.page(limit=1)) == 1
    with ImportInventory(path) as reopened:
        assert reopened.sealed
        with pytest.raises(RuntimeError, match="sealed"):
            reopened.append([_item(2)])


def test_append_reserves_write_before_sealed_check_across_connections(tmp_path: Path):
    path = tmp_path / "inventory.sqlite3"
    first = ImportInventory(path)
    second = ImportInventory(path)
    second._db.execute("PRAGMA busy_timeout = 0")

    def item_while_competing_seal():
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            second.seal()
        yield _item(0)

    try:
        first.append(item_while_competing_seal())
        assert first.totals() == {"count": 1, "bytes": 10}
        second.seal()
        with pytest.raises(RuntimeError, match="sealed"):
            first.append([_item(1)])
    finally:
        first.close()
        second.close()


def test_large_streaming_append_and_page_use_summary_and_keyset_sql(tmp_path: Path):
    path = tmp_path / "inventory.sqlite3"
    yielded = 0

    def items():
        nonlocal yielded
        for index in range(10_000):
            yielded += 1
            yield _item(index, size=1)

    with ImportInventory(path) as inventory:
        inventory.append(items())
        assert yielded == 10_000
        statements: list[str] = []
        inventory._db.set_trace_callback(statements.append)
        assert inventory.totals() == {"count": 10_000, "bytes": 10_000}
        rows = inventory.page(after=9_990, limit=5)
        inventory._db.set_trace_callback(None)
        assert [row["ordinal"] for row in rows] == [9991, 9992, 9993, 9994, 9995]
        sql = " ".join(statements).upper()
        assert "COUNT(" not in sql and "SUM(" not in sql
        assert "WHERE ORDINAL >" in sql and "OFFSET" not in sql


def test_terminal_compaction_reclaims_items_but_keeps_summary_and_batch_replay(
    tmp_path: Path,
):
    path = tmp_path / "inventory.sqlite3"
    first_batch = [
        {**_item(index, size=1), "logical_path": f"{'nested/' * 80}{index}.eml"}
        for index in range(100)
    ]
    with ImportInventory(path) as inventory:
        through, added = append_inventory_batch(inventory, "first", first_batch)
        assert (through, added) == (100, True)
        for start in range(100, 20_000, 100):
            append_inventory_batch(
                inventory,
                f"batch-{start}",
                [
                    {
                        **_item(index, size=1),
                        "logical_path": f"{'nested/' * 80}{index}.eml",
                    }
                    for index in range(start, start + 100)
                ],
            )
        inventory.seal()
    before = path.stat().st_size

    with ImportInventory(path) as inventory:
        inventory.compact_terminal()
        assert inventory.totals() == {"count": 20_000, "bytes": 20_000}
        assert inventory.through == 20_000
        assert inventory.sealed
        assert inventory.page(limit=1) == []
        assert inventory_batch_through(inventory, "first", first_batch) == 100
        with pytest.raises(ValueError, match="different files"):
            inventory_batch_through(inventory, "first", [{**first_batch[0], "size": 2}])
        with pytest.raises(RuntimeError, match="sealed"):
            inventory.append([_item(20_001)])

    after = path.stat().st_size
    assert before > 5 * after, (before, after)


def test_terminal_compaction_refuses_an_active_inventory(tmp_path: Path):
    with ImportInventory(tmp_path / "inventory.sqlite3") as inventory:
        inventory.append([_item(0)])
        with pytest.raises(RuntimeError, match="sealed"):
            inventory.compact_terminal()
        assert len(inventory.page(limit=1)) == 1


def test_reopen_migrates_existing_inventory_through_ordinal(tmp_path: Path):
    path = tmp_path / "inventory.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE inventory_meta (
            singleton INTEGER PRIMARY KEY,
            item_count INTEGER NOT NULL,
            total_bytes INTEGER NOT NULL,
            sealed INTEGER NOT NULL
        );
        INSERT INTO inventory_meta VALUES (1,2,20,1);
        CREATE TABLE inventory_items (
            ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
            logical_path TEXT NOT NULL,
            mime TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size INTEGER NOT NULL,
            kind TEXT NOT NULL,
            email_format TEXT
        );
        INSERT INTO inventory_items
            (logical_path,mime,sha256,size,kind,email_format)
            VALUES ('one','text/plain','0000000000000000000000000000000000000000000000000000000000000000',10,'files',NULL),
                   ('two','text/plain','1111111111111111111111111111111111111111111111111111111111111111',10,'files',NULL);
        """
    )
    db.close()

    with ImportInventory(path) as inventory:
        assert inventory.through == 2
        assert inventory.totals() == {"count": 2, "bytes": 20}
