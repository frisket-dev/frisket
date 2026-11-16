"""Build historical test bundles from frozen DDL, independently of current schema.

Fixtures may seed ordinary data through the current facade, but must then use
real old table definitions before testing an upgrade. This is a test-only
conversion for representable fixture data, not a downgrade supported by Frisket.
"""

from __future__ import annotations

import gzip
from pathlib import Path
import sqlite3


PRE_DOMAIN_SCHEMA = gzip.decompress(
    (Path(__file__).parents[1] / "fixtures/domain-v0-schema.sql.gz").read_bytes()
).decode()
PRE_DOMAIN_DIGEST = "frisket.schema.v1:7e339a8995763aa40346e7d36187837d"


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def restore_pre_domain_schema(db: sqlite3.Connection) -> None:
    """Preserve fixture data while replacing every object with the frozen schema.

    New-only tables/columns cannot leak into tests of prior migrations. Old
    constraints still apply: a fixture using unrepresentable new features fails
    here rather than being silently coerced into fake historical data.
    """
    if "change_order" not in {row[1] for row in db.execute("PRAGMA table_info(ops)")}:
        return
    from frisket.engine.store.current_cells import register_current_cell_functions

    register_current_cell_functions(db)
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    old = sqlite3.connect(":memory:")
    try:
        old.executescript(PRE_DOMAIN_SCHEMA.replace("PRAGMA foreign_keys=ON;", ""))
        tables = [
            row[0]
            for row in old.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        saved = {}
        for table in tables:
            columns = [
                row[1] for row in old.execute(f"PRAGMA table_info({_quoted(table)})")
            ]
            available = {
                row[1] for row in db.execute(f"PRAGMA table_info({_quoted(table)})")
            }
            common = [name for name in columns if name in available]
            if not common:
                continue
            names = ",".join(map(_quoted, common))
            records = [
                list(row) for row in db.execute(f"SELECT {names} FROM {_quoted(table)}")
            ]
            if table == "current_cells":
                # The prior layer represented source ownership through producer_id,
                # not origin_op_id; values and producer identities stay untouched.
                for record in records:
                    if record[common.index("origin_kind")] == "source_cell":
                        record[common.index("origin_op_id")] = None
            saved[table] = (common, records)
        # Preserve logical run heads, including those underneath manual edits.
        names = ["column_id", "row_id", "run_id"]
        heads = {
            tuple(row)
            for row in db.execute(
                "SELECT column_id,row_id,run_id FROM generated_result_candidates"
            )
        }
        heads.update(
            tuple(row)
            for row in db.execute(
                "SELECT column_id,row_id,run_id FROM cell_result_heads"
            )
        )
        saved["cell_result_heads"] = (names, sorted(heads))
    finally:
        old.close()

    for kind in ("view", "trigger", "table"):
        objects = db.execute(
            "SELECT name FROM sqlite_master WHERE type=? AND name NOT LIKE 'sqlite_%'",
            (kind,),
        ).fetchall()
        for (name,) in objects:
            db.execute(f"DROP {kind.upper()} {_quoted(name)}")
    db.executescript(PRE_DOMAIN_SCHEMA.replace("PRAGMA foreign_keys=ON;", ""))
    triggers = db.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
    ).fetchall()
    for name, _ in triggers:
        db.execute(f"DROP TRIGGER {_quoted(name)}")
    for table, (columns, records) in saved.items():
        db.execute(f"DELETE FROM {_quoted(table)}")
        if records:
            names = ",".join(map(_quoted, columns))
            db.executemany(
                f"INSERT INTO {_quoted(table)} ({names}) VALUES ({','.join('?' for _ in columns)})",
                records,
            )
    for _, statement in triggers:
        db.execute(statement)
    db.execute(
        "DELETE FROM meta WHERE key IN ('change_sequence', 'domain_history_floor')"
    )
    db.execute(
        "UPDATE meta SET value=? WHERE key='schema_digest'", (PRE_DOMAIN_DIGEST,)
    )
    db.commit()
