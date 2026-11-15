"""Keep derivation identity without making provenance own source bytes."""

from __future__ import annotations

import sqlite3


def migrate_blob_derivation_ownership(db: sqlite3.Connection) -> None:
    """Run inside the project upgrade transaction, before blob reclamation.

    Only the derived object owns this record. ``source_hash`` remains an
    historical locator after the source is reclaimed; it isn't a blob lease.
    """
    if not db.in_transaction:
        raise ValueError("blob ownership migration requires a transaction")
    if not any(
        row[3] == "source_hash"
        for row in db.execute("PRAGMA foreign_key_list(blob_derivations)")
    ):
        return
    high_water = db.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='blob_derivations'"
    ).fetchone()
    db.execute(
        "CREATE TABLE blob_derivations_retention_new ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "derived_hash TEXT NOT NULL REFERENCES blobs(hash) ON DELETE CASCADE,"
        "source_hash TEXT NOT NULL,op TEXT NOT NULL,"
        "params_json TEXT NOT NULL DEFAULT '{}',"
        "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    db.execute(
        "INSERT INTO blob_derivations_retention_new "
        "SELECT id,derived_hash,source_hash,op,params_json,created_at FROM blob_derivations"
    )
    db.execute("DROP TABLE blob_derivations")
    db.execute("ALTER TABLE blob_derivations_retention_new RENAME TO blob_derivations")
    if high_water is not None:
        updated = db.execute(
            "UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name='blob_derivations'",
            (high_water[0],),
        ).rowcount
        if not updated:
            db.execute(
                "INSERT INTO sqlite_sequence(name,seq) VALUES ('blob_derivations',?)",
                (high_water[0],),
            )
    db.execute(
        "CREATE INDEX idx_blob_derivations_derived ON blob_derivations(derived_hash)"
    )
    db.execute(
        "CREATE INDEX idx_blob_derivations_source ON blob_derivations(source_hash)"
    )
