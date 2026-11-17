from __future__ import annotations


from frisket.engine.store.materialization import (
    EdgeTableColumnSpec,
    EdgeTablePlan,
    MaterializedEdgeRecord,
    active_materialized_row_sources,
    materialized_row_source_diagnostics,
    materialized_row_sources_ref_matches,
    write_edge_table,
)


def test_materialization_store_writes_and_reads_active_membership(
    make_project,
) -> None:
    project = make_project("Mat")
    source_sheet_id = project.add_sheet("People")
    name_col = project.add_column(source_sheet_id, "name", type="text")
    row_ids = project.add_rows(
        source_sheet_id,
        [{"name": "Alice"}, {"name": "Bob"}],
        {"name": name_col},
    )

    cur = project.db.cursor()
    cur.execute("BEGIN IMMEDIATE")
    write = write_edge_table(
        cur,
        EdgeTablePlan(
            action_kind="test.edge_table",
            label="edge table",
            target_sheet_name="Edges",
            parent_sheet_id=source_sheet_id,
            op_spec={"kind": "test.edge_table"},
            columns=[EdgeTableColumnSpec("label", "text")],
            edges=[
                MaterializedEdgeRecord(
                    source_row_id=row_ids[0],
                    target_row_id=row_ids[1],
                    values={"label": "knows"},
                )
            ],
        ),
    )
    project.db.commit()

    active = active_materialized_row_sources(project.db.cursor())
    assert active == write.membership_rows
    assert materialized_row_sources_ref_matches(
        write.materialized_row_sources_ref,
        active,
    )
    assert materialized_row_source_diagnostics(project.db.cursor()) == []

    project.db.execute("UPDATE rows SET hidden=1 WHERE id=?", (write.row_ids[0],))
    project.db.commit()
    assert active_materialized_row_sources(project.db.cursor()) == []
    historical = active_materialized_row_sources(
        project.db.cursor(),
        include_historical=True,
    )
    assert historical == write.membership_rows


def test_refresh_versions_group_contributors_and_undo_restores_prior_set(make_project):
    from frisket.engine.store.derived_updates import (
        MaterializedIdentity,
        DerivedRefreshRow,
        reconcile_rows_into_open_change,
        refresh_config,
    )
    from frisket.engine.store.materialization import (
        ContributorTablePlan,
        MaterializedColumnSpec,
        ContributorMaterializedRow,
        write_contributor_table,
    )

    project = make_project("group-membership")
    source = project.add_sheet("Source")
    col = project.add_column(source, "name", type="text")
    source_rows = project.add_rows(
        source, [{"name": name} for name in ["a", "b", "c"]], {"name": col}
    )
    project.db.execute("BEGIN IMMEDIATE")
    columns = [MaterializedColumnSpec("group", "text")]
    write = write_contributor_table(
        project.db.cursor(),
        ContributorTablePlan(
            action_kind="test.group",
            label="group",
            target_sheet_name="Groups",
            parent_sheet_id=source,
            op_spec={},
            columns=columns,
            rows=[
                ContributorMaterializedRow(
                    values={"group": "one"},
                    sources=[(row, "aggregate_source") for row in source_rows[:2]],
                )
            ],
            refresh_identity=MaterializedIdentity("group", ["one"]),
        ),
    )
    project.db.commit()
    project.db.execute("BEGIN IMMEDIATE")
    config = refresh_config(
        project.db.execute("SELECT * FROM ops WHERE id=?", (write.op_id,)).fetchone()
    )
    from frisket.engine.store.changes import ChangeStore

    refresh_op = project.append_op(
        "sheet.refresh", {"sheet_id": write.sheet_id}, commit=False
    )
    changes = ChangeStore(project)
    changes.start(refresh_op)
    new_ids = reconcile_rows_into_open_change(
        project.db,
        op_id=refresh_op,
        sheet_id=write.sheet_id,
        config=config,
        columns=columns,
        rows=[
            DerivedRefreshRow(
                values={"group": "one"},
                key="one",
                sources=[(row, "aggregate_source") for row in source_rows[1:]],
            )
        ],
    )
    changes.close(refresh_op)
    project.db.commit()
    assert new_ids == write.row_ids
    current = active_materialized_row_sources(
        project.db.cursor(), materialized_row_ids=new_ids
    )
    assert [row["source_row_id"] for row in current] == source_rows[1:]
    project.undo()
    restored = active_materialized_row_sources(
        project.db.cursor(), materialized_row_ids=new_ids
    )
    assert [row["source_row_id"] for row in restored] == source_rows[:2]
    assert (
        len(
            active_materialized_row_sources(
                project.db.cursor(),
                materialized_row_ids=new_ids,
                include_historical=True,
            )
        )
        == 4
    )


def test_materialized_lineage_upgrade_preserves_edges_and_foreign_keys():
    import sqlite3
    from frisket.engine.store.materialized_lineage import (
        MATERIALIZED_LINEAGE_SCHEMA_SQL,
    )
    from frisket.upgrades.project.domain_identity_migration import (
        migrate_materialized_lineage,
    )

    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE sheets(id INTEGER PRIMARY KEY);
        CREATE TABLE rows(id INTEGER PRIMARY KEY);
        CREATE TABLE ops(id INTEGER PRIMARY KEY,status TEXT,change_order INTEGER);
        INSERT INTO sheets VALUES(1);
        INSERT INTO rows VALUES(1),(2);
        INSERT INTO ops VALUES(1,'applied',1);
    """)
    old_schema = MATERIALIZED_LINEAGE_SCHEMA_SQL.split("CREATE VIEW")[0].replace(
        "PRIMARY KEY (materialized_row_id, source_row_id, role, op_id)",
        "PRIMARY KEY (materialized_row_id, source_row_id, role)",
    )
    db.executescript(old_schema)
    db.execute(
        "INSERT INTO materialized_row_sources VALUES(2,1,1,1,'aggregate_source')"
    )
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    migrate_materialized_lineage(db)
    assert db.in_transaction
    assert db.execute("SELECT * FROM current_materialized_row_sources").fetchall() == [
        (2, 1, 1, 1, "aggregate_source")
    ]
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert (
        len(
            [
                row
                for row in db.execute("PRAGMA table_info(materialized_row_sources)")
                if row[5]
            ]
        )
        == 4
    )
    db.rollback()
    assert (
        len(
            [
                row
                for row in db.execute("PRAGMA table_info(materialized_row_sources)")
                if row[5]
            ]
        )
        == 3
    )
    db.close()
