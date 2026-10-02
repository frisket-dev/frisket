from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from frisket.engine.store.import_inventory import ImportInventory


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
