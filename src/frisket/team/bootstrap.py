"""Open-identity control-plane bootstrap.

Fresh initialization of the identity schema (`frisket.team.schema`), plus the
queryable schema-identity marker that every edition stamps.

Clean cut, then migrations
---------------------------
The identity/commerce split was a pre-release clean cut: a database that
predates it (identity and commerce fused on one `orgs` table) is still
REJECTED rather than guessed at.  Once the split schema exists, however,
versioned upgrades preserve its data in place (v6 -> v7 adds the user
allowance column; v7 -> v8 renames it to `allowance_usd`; v8 -> v9 renames
the organization tables and foreign-key columns to `organizations` /
`organization_id`).

`ensure_clean_control_plane_schema` is that gate, and it is shared: an
external hosted composition runs it too, via its own bootstrap module, so
both editions refuse a pre-split database identically.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

from frisket.team.schema import metadata

# Bumped only when the identity schema changes shape in a way that an existing
# database cannot serve. The split itself is version 1: there is no version 0
# to upgrade from, because pre-split databases are dropped, not migrated.
SCHEMA_VERSION = 9

TEAM_EDITION = "team"

# The marker is bootstrap bookkeeping that every edition stamps — not identity
# schema — and two normative claims bound where it may live:
#
#   * it is NOT in frisket.team.schema.metadata: the identity metadata owns
#     exactly the identity tables;
#   * it does NOT add a second schema registry to the open identity package,
#     which exports exactly one — the one that `create_all` must be able to
#     build an identity-only database from.
#
# So the marker carries its own separate registry, reachable through the table
# itself (`schema_marker.metadata`). An external composition re-exports that
# registry as `marker_metadata` (in its own bootstrap module) to keep the
# whole schema surface discoverable from the hosted bootstrap composition.
schema_marker = sa.Table(
    "schema_marker",
    sa.MetaData(),
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("edition", sa.String, nullable=False),
    sa.Column("schema_version", sa.Integer, nullable=False),
    sa.Column("stamped_at", sa.DateTime(timezone=True)),
)


class PreSplitSchemaError(RuntimeError):
    """A control-plane database that predates this schema's split from
    commerce policy."""


def _upgrade_v6_to_v7(engine: sa.Engine, *, edition: str | None) -> bool:
    """Add the user-owned allowance field without losing identity data."""

    with engine.begin() as cx:
        marker = cx.execute(sa.select(schema_marker)).all()
        if (
            len(marker) != 1
            or marker[0].id != 1
            or marker[0].schema_version != 6
            or (edition is not None and marker[0].edition != edition)
        ):
            return False
        tables = set(sa.inspect(cx).get_table_names())
        if "users" not in tables:
            return False
        columns = {column["name"] for column in sa.inspect(cx).get_columns("users")}
        if "cost_preapproval_usd" not in columns and "allowance_usd" not in columns:
            cx.execute(
                sa.text(
                    "ALTER TABLE users ADD COLUMN cost_preapproval_usd NUMERIC(18, 6)"
                )
            )
        cx.execute(
            schema_marker.update()
            .where(schema_marker.c.id == 1)
            .values(schema_version=7, stamped_at=datetime.now(UTC))
        )
    return True


def _upgrade_v7_to_v8(engine: sa.Engine, *, edition: str | None) -> bool:
    """Rename the user allowance column in place without losing identity data."""

    with engine.begin() as cx:
        marker = cx.execute(sa.select(schema_marker)).all()
        if (
            len(marker) != 1
            or marker[0].id != 1
            or marker[0].schema_version != 7
            or (edition is not None and marker[0].edition != edition)
        ):
            return False
        tables = set(sa.inspect(cx).get_table_names())
        if "users" not in tables:
            return False
        columns = {column["name"] for column in sa.inspect(cx).get_columns("users")}
        if "allowance_usd" not in columns:
            if "cost_preapproval_usd" in columns:
                cx.execute(
                    sa.text(
                        "ALTER TABLE users "
                        "RENAME COLUMN cost_preapproval_usd TO allowance_usd"
                    )
                )
            else:
                cx.execute(
                    sa.text("ALTER TABLE users ADD COLUMN allowance_usd NUMERIC(18, 6)")
                )
        cx.execute(
            schema_marker.update()
            .where(schema_marker.c.id == 1)
            .values(schema_version=8, stamped_at=datetime.now(UTC))
        )
    return True


_V8_TO_V9_TABLE_RENAMES: tuple[tuple[str, str], ...] = (
    ("orgs", "organizations"),
    ("org_keys", "organization_keys"),
    ("org_env_vars", "organization_env_vars"),
)

_V8_TO_V9_COLUMN_RENAMES: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("users", (("default_org_id", "default_organization_id"),)),
    ("memberships", (("org_id", "organization_id"),)),
    (
        "projects",
        (
            ("org_id", "organization_id"),
            ("storage_org_id", "storage_organization_id"),
        ),
    ),
    ("pending_invites", (("org_id", "organization_id"),)),
    ("project_invites", (("org_id", "organization_id"),)),
    ("project_roles", (("org_id", "organization_id"),)),
    ("project_creation_intents", (("org_id", "organization_id"),)),
    ("api_tokens", (("org_id", "organization_id"),)),
    ("operator_tokens", (("org_id", "organization_id"),)),
    ("audit_log", (("org_id", "organization_id"),)),
    ("client_errors", (("org_id", "organization_id"),)),
    ("organization_keys", (("org_id", "organization_id"),)),
    ("organization_env_vars", (("org_id", "organization_id"),)),
    ("oauth_connections", (("org_id", "organization_id"),)),
)


def _upgrade_v8_to_v9(engine: sa.Engine, *, edition: str | None) -> bool:
    """Rename organization tables and columns in place without losing data."""

    with engine.begin() as cx:
        marker = cx.execute(sa.select(schema_marker)).all()
        if (
            len(marker) != 1
            or marker[0].id != 1
            or marker[0].schema_version != 8
            or (edition is not None and marker[0].edition != edition)
        ):
            return False
        tables = set(sa.inspect(cx).get_table_names())
        if "users" not in tables:
            return False
        for old_table, new_table in _V8_TO_V9_TABLE_RENAMES:
            if old_table in tables and new_table not in tables:
                cx.execute(sa.text(f"ALTER TABLE {old_table} RENAME TO {new_table}"))
                tables.remove(old_table)
                tables.add(new_table)
        for table, renames in _V8_TO_V9_COLUMN_RENAMES:
            if table not in tables:
                continue
            columns = {column["name"] for column in sa.inspect(cx).get_columns(table)}
            for old_col, new_col in renames:
                if old_col in columns and new_col not in columns:
                    cx.execute(
                        sa.text(
                            f"ALTER TABLE {table} RENAME COLUMN {old_col} TO {new_col}"
                        )
                    )
                    columns.remove(old_col)
                    columns.add(new_col)
        cx.execute(
            schema_marker.update()
            .where(schema_marker.c.id == 1)
            .values(schema_version=9, stamped_at=datetime.now(UTC))
        )
    return True


def ensure_clean_control_plane_schema(
    engine: sa.Engine, *, edition: str | None = None
) -> None:
    """Refuse to operate on anything but a blank or already-split database.

    Blank engine  -> fine, the caller initializes it.
    Marked engine -> fine, it was initialized by this code.
    Anything else -> PreSplitSchemaError. In particular a pre-split database,
    whose `orgs` still carries credits_micro/grace_micro/quota_* and which has
    no marker, is never silently migrated or read through a compatibility
    shim.
    """
    existing = set(sa.inspect(engine).get_table_names())
    if not existing:
        return
    if schema_marker.name in existing:
        with engine.connect() as cx:
            markers = cx.execute(sa.select(schema_marker)).all()
        if (
            len(markers) == 1
            and markers[0].id == 1
            and (edition is None or markers[0].edition == edition)
        ):
            version = markers[0].schema_version
            if version == SCHEMA_VERSION:
                return
            if version == 6 and _upgrade_v6_to_v7(engine, edition=edition):
                version = 7
            if version == 7 and _upgrade_v7_to_v8(engine, edition=edition):
                version = 8
            if version == 8 and _upgrade_v8_to_v9(engine, edition=edition):
                return
    raise PreSplitSchemaError(
        "control-plane database was not created by the current schema "
        f"(missing or incompatible {schema_marker.name!r}; expected "
        f"version={SCHEMA_VERSION}, tables={sorted(existing)}). "
        "The identity / commerce-policy schema split is a clean cut: pre-split "
        "databases (orgs carrying credits_micro / "
        "grace_micro / quota_*) cannot be upgraded in place. Reset the control "
        "plane — drop the database (or delete the SQLite file) and reseed it "
        "with a fresh init."
    )


def init_identity_schema(engine: sa.Engine, *, edition: str = TEAM_EDITION) -> None:
    """Create the identity schema on a blank engine and stamp the marker.

    Idempotent: running it against a database this function already initialized
    is a no-op. Running it against a pre-split database raises
    PreSplitSchemaError (see `ensure_clean_control_plane_schema`).

    Creates NO commerce tables — an external managed edition layers those on
    top via its own bootstrap module.
    """
    marker_table_was_absent = schema_marker.name not in set(
        sa.inspect(engine).get_table_names()
    )
    ensure_clean_control_plane_schema(engine, edition=edition)
    metadata.create_all(engine)
    schema_marker.metadata.create_all(engine)
    _stamp_schema_marker(
        engine,
        edition=edition,
        allow_empty=marker_table_was_absent,
    )


def _stamp_schema_marker(engine: sa.Engine, *, edition: str, allow_empty: bool) -> None:
    with engine.begin() as cx:
        markers = cx.execute(sa.select(schema_marker)).all()
        if not markers and not allow_empty:
            raise PreSplitSchemaError(
                "control-plane schema marker is empty; "
                "the pre-release clean cut requires drop and reseed"
            )
        if markers:
            if (
                len(markers) != 1
                or markers[0].id != 1
                or markers[0].edition != edition
                or markers[0].schema_version != SCHEMA_VERSION
            ):
                raise PreSplitSchemaError(
                    "control-plane schema marker does not match "
                    f"edition={edition!r} version={SCHEMA_VERSION}; "
                    "the pre-release clean cut requires drop and reseed"
                )
            return
        values = {
            "id": 1,
            "edition": edition,
            "schema_version": SCHEMA_VERSION,
            "stamped_at": datetime.now(UTC),
        }
        if cx.dialect.name == "sqlite":
            cx.execute(schema_marker.insert().prefix_with("OR IGNORE").values(**values))
        elif cx.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert

            cx.execute(insert(schema_marker).values(**values).on_conflict_do_nothing())
        else:
            cx.execute(schema_marker.insert().values(**values))
        stamped = cx.execute(
            sa.select(
                schema_marker.c.edition,
                schema_marker.c.schema_version,
            ).where(schema_marker.c.id == 1)
        ).one()
        if stamped.edition != edition or stamped.schema_version != SCHEMA_VERSION:
            raise PreSplitSchemaError(
                "control-plane schema marker does not match "
                f"edition={edition!r} version={SCHEMA_VERSION}; drop and reseed"
            )
