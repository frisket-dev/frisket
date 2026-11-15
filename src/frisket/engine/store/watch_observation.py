"""Bounded, disposable staging for one coherent alert observation."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from itertools import islice
from pathlib import Path
from typing import Any


class WatchObservationIncomplete(ValueError):
    pass


@contextmanager
def stage_watch_observation(
    project: Any,
    hits: Iterable[dict[str, Any]],
    fields: list[dict[str, Any]],
) -> Iterator[Path]:
    """Consume membership and tracked cells from the caller's read snapshot.

    Only latest membership and configured fields are staged. A private disk DB
    bounds Python memory and disappears on success, failure or cancellation.
    """
    for field in fields:
        column = project.db.execute(
            "SELECT sheet_id FROM columns WHERE id=? AND active=1",
            (int(field["column_id"]),),
        ).fetchone()
        if column is None or int(column[0]) != int(field["sheet_id"]):
            raise WatchObservationIncomplete("A monitored column is unavailable.")
    with tempfile.TemporaryDirectory(prefix="frisket-watch-") as directory:
        path = Path(directory) / "observation.sqlite"
        stage = sqlite3.connect(path)
        try:
            stage.executescript("""
                CREATE TABLE members (
                    sheet_id INTEGER NOT NULL,row_id INTEGER NOT NULL,
                    column_id INTEGER,rank INTEGER NOT NULL,snippet TEXT,
                    PRIMARY KEY(sheet_id,row_id)
                ) WITHOUT ROWID;
                CREATE TABLE fields (
                    sheet_id INTEGER NOT NULL,row_id INTEGER NOT NULL,
                    column_id INTEGER NOT NULL,value_json TEXT NOT NULL,value_hash TEXT NOT NULL,
                    PRIMARY KEY(sheet_id,row_id,column_id)
                ) WITHOUT ROWID;
            """)
            iterator = iter(hits)
            rank = 0
            while batch := list(islice(iterator, 500)):
                grouped: dict[int, list[int]] = {}
                for hit in batch:
                    rank += 1
                    sheet, row = int(hit["sheet_id"]), int(hit["row_id"])
                    stage.execute(
                        "INSERT OR IGNORE INTO members VALUES (?,?,?,?,?)",
                        (sheet, row, hit.get("column_id"), rank, hit.get("snippet")),
                    )
                    grouped.setdefault(sheet, []).append(row)
                for field in fields:
                    sheet, column = int(field["sheet_id"]), int(field["column_id"])
                    rows = grouped.get(sheet, [])
                    if not rows:
                        continue
                    placeholders = ",".join("?" for _ in rows)
                    states = {
                        int(cell["row_id"]): cell["outcome"]
                        for cell in project.db.execute(
                            "SELECT row_id,outcome FROM current_cell_values WHERE column_id=? "
                            f"AND row_id IN ({placeholders})",
                            (column, *rows),
                        )
                    }
                    if "pending" in states.values():
                        raise WatchObservationIncomplete(
                            "A monitored value is not available yet."
                        )
                    values = project.get_values(
                        sheet, column, row_ids=rows, preserve_invalid=True
                    )
                    stage.executemany(
                        "INSERT OR REPLACE INTO fields VALUES (?,?,?,?,?)",
                        [
                            (
                                sheet,
                                row,
                                column,
                                value,
                                "sha256:" + hashlib.sha256(value.encode()).hexdigest(),
                            )
                            for row in rows
                            for value in [
                                json.dumps(
                                    {
                                        "outcome": states.get(row, "empty"),
                                        "value": values.get(row),
                                    },
                                    ensure_ascii=False,
                                    sort_keys=True,
                                )
                            ]
                        ],
                    )
                stage.commit()
            yield path
        finally:
            stage.close()
