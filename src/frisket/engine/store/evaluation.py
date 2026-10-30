"""Small project-local stores for live expected links and immutable comparison output."""

from __future__ import annotations

import json
from contextlib import contextmanager

EVALUATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS expected_answer_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
  expected_sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
  action_id TEXT NOT NULL,
  keys_json TEXT NOT NULL CHECK (json_valid(keys_json)),
  fields_json TEXT NOT NULL CHECK (json_valid(fields_json)),
  hidden INTEGER NOT NULL DEFAULT 0 CHECK (hidden IN (0,1))
);
CREATE INDEX IF NOT EXISTS idx_expected_answer_links_target
  ON expected_answer_links(target_sheet_id, action_id);
CREATE TABLE IF NOT EXISTS expected_answer_rules (
  expected_column_id INTEGER PRIMARY KEY REFERENCES columns(id) ON DELETE CASCADE,
  rule TEXT NOT NULL CHECK (rule IN ('exact','ignore_case','number','date','set','hand'))
);
CREATE TABLE IF NOT EXISTS evaluation_judgments (
  sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
  row_id INTEGER NOT NULL REFERENCES rows(id) ON DELETE CASCADE,
  expected_column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  output_column_id INTEGER NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
  source_fingerprint TEXT NOT NULL,
  expected_hash TEXT NOT NULL,
  answer_hash TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK (verdict IN ('match','miss')),
  actor TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (sheet_id,row_id,expected_column_id,output_column_id,
               source_fingerprint,expected_hash,answer_hash)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS saved_compare_variants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
  action_id TEXT NOT NULL,
  label TEXT NOT NULL,
  output_columns_json TEXT NOT NULL CHECK (json_valid(output_columns_json)),
  settings_json TEXT NOT NULL CHECK (json_valid(settings_json)),
  rows_json TEXT NOT NULL CHECK (json_valid(rows_json)),
  fingerprints_json TEXT NOT NULL CHECK (json_valid(fingerprints_json)),
  kept INTEGER NOT NULL DEFAULT 0 CHECK (kept IN (0,1)),
  saved_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_saved_compare_variants_group
  ON saved_compare_variants(sheet_id,action_id,id);
"""


def dump(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@contextmanager
def write_transaction(project):
    """Commit one editor operation; preserve a surrounding caller transaction."""
    db = project.db
    outer = db.in_transaction
    db.execute("SAVEPOINT evaluation_write" if outer else "BEGIN IMMEDIATE")
    try:
        yield
        db.execute("RELEASE evaluation_write") if outer else db.commit()
    except BaseException:
        if outer:
            db.execute("ROLLBACK TO evaluation_write")
            db.execute("RELEASE evaluation_write")
        else:
            db.rollback()
        raise


def link(project, link_id):
    row = project.db.execute(
        "SELECT * FROM expected_answer_links WHERE id=?", (link_id,)
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["keys"] = json.loads(result.pop("keys_json"))
    result["fields"] = json.loads(result.pop("fields_json"))
    result["hidden"] = bool(result["hidden"])
    for field in result["fields"]:
        setting = project.db.execute(
            "SELECT rule FROM expected_answer_rules WHERE expected_column_id=?",
            (field["expected_column_id"],),
        ).fetchone()
        field["rule"] = setting[0] if setting else "exact"
    return result


def links(project, sheet_id):
    return [
        link(project, row[0])
        for row in project.db.execute(
            "SELECT id FROM expected_answer_links WHERE target_sheet_id=? ORDER BY id",
            (sheet_id,),
        )
    ]


def save_link(project, body, link_id=None):
    fields = [
        {key: value for key, value in field.items() if key != "rule"}
        for field in body["fields"]
    ]
    values = (
        body["target_sheet_id"],
        body["expected_sheet_id"],
        body["action_id"],
        dump(body["keys"]),
        dump(fields),
        int(body["hidden"]),
    )
    if link_id is None:
        cursor = project.db.execute(
            "INSERT INTO expected_answer_links(target_sheet_id,expected_sheet_id,action_id,keys_json,fields_json,hidden) VALUES (?,?,?,?,?,?)",
            values,
        )
        link_id = cursor.lastrowid
    else:
        project.db.execute(
            "UPDATE expected_answer_links SET target_sheet_id=?,expected_sheet_id=?,action_id=?,keys_json=?,fields_json=?,hidden=? WHERE id=?",
            (*values, link_id),
        )
    for field in body["fields"]:
        project.db.execute(
            "INSERT INTO expected_answer_rules(expected_column_id,rule) VALUES (?,?) ON CONFLICT(expected_column_id) DO NOTHING",
            (field["expected_column_id"], field["rule"]),
        )
    return link_id


def set_rule(project, column_id, rule):
    project.db.execute(
        "INSERT INTO expected_answer_rules(expected_column_id,rule) VALUES (?,?) ON CONFLICT(expected_column_id) DO UPDATE SET rule=excluded.rule",
        (column_id, rule),
    )


def judgment(project, identity):
    where = " AND ".join(f"{key}=?" for key in identity)
    row = project.db.execute(
        f"SELECT verdict FROM evaluation_judgments WHERE {where}",
        tuple(identity.values()),
    ).fetchone()
    return row[0] if row else None


def save_judgment(project, identity, verdict, actor):
    if verdict is None:
        where = " AND ".join(f"{key}=?" for key in identity)
        project.db.execute(
            f"DELETE FROM evaluation_judgments WHERE {where}", tuple(identity.values())
        )
    else:
        keys = list(identity)
        project.db.execute(
            f"INSERT INTO evaluation_judgments ({','.join(keys)},verdict,actor) VALUES ({','.join('?' for _ in keys)},?,?) ON CONFLICT DO UPDATE SET verdict=excluded.verdict,actor=excluded.actor,updated_at=datetime('now')",
            (*identity.values(), verdict, actor),
        )


def saved(project, saved_id):
    row = project.db.execute(
        "SELECT * FROM saved_compare_variants WHERE id=?", (saved_id,)
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    for stored, public in (
        ("output_columns_json", "output_columns"),
        ("settings_json", "settings"),
        ("rows_json", "rows"),
        ("fingerprints_json", "source_fingerprints"),
    ):
        result[public] = json.loads(result.pop(stored))
    result["row_count"] = len(result["rows"])
    result["kept"] = bool(result["kept"])
    return result


def save_variant(
    project,
    *,
    sheet_id,
    action_id,
    label,
    output_columns,
    settings,
    rows,
    source_fingerprints,
    kept=True,
):
    cursor = project.db.execute(
        "INSERT INTO saved_compare_variants(sheet_id,action_id,label,output_columns_json,settings_json,rows_json,fingerprints_json,kept) VALUES (?,?,?,?,?,?,?,?)",
        (
            sheet_id,
            action_id,
            label,
            dump(output_columns),
            dump(settings),
            dump(rows),
            dump(source_fingerprints),
            int(kept),
        ),
    )
    return cursor.lastrowid
