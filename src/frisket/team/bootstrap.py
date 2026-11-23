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
`organization_id`; v9 -> v10 adds the creator's Allowed services,
usage-data choices and cover seed to project creation intents; v10 -> v11
folds the network on/off defaults into Allowed services: an organization
default of off becomes Approved services, creation intents without their
own Allowed services take that default, and the network columns are
dropped).

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
SCHEMA_VERSION = 11

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


def _apply_v6_to_v7(cx: sa.Connection) -> None:
    """Add the user-owned allowance field without losing identity data."""
    columns = {column["name"] for column in sa.inspect(cx).get_columns("users")}
    if "cost_preapproval_usd" not in columns and "allowance_usd" not in columns:
        cx.execute(
            sa.text("ALTER TABLE users ADD COLUMN cost_preapproval_usd NUMERIC(18, 6)")
        )


def _apply_v7_to_v8(cx: sa.Connection) -> None:
    """Rename the user allowance column in place without losing identity data."""
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


def _apply_v8_to_v9(cx: sa.Connection, tables: set[str]) -> None:
    """Rename organization tables and columns in place without losing data."""
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
                    sa.text(f"ALTER TABLE {table} RENAME COLUMN {old_col} TO {new_col}")
                )
                columns.remove(old_col)
                columns.add(new_col)


def _apply_v9_to_v10(cx: sa.Connection, tables: set[str]) -> None:
    """Add the creation-intent settings columns; existing intents keep NULL.

    A database without the intents table gets it whole from ``create_all``.
    """
    if "project_creation_intents" not in tables:
        return
    columns = {
        column["name"]
        for column in sa.inspect(cx).get_columns("project_creation_intents")
    }
    for name in ("allowed_services", "usage_data_sharing", "cover_seed"):
        if name not in columns:
            cx.execute(
                sa.text(
                    f"ALTER TABLE project_creation_intents ADD COLUMN {name} VARCHAR"
                )
            )


def _apply_v10_to_v11(cx: sa.Connection, tables: set[str]) -> None:
    """Carry the organization network default into Allowed services.

    Off becomes Approved services and on becomes All services; an unset
    default stays unset (All services). A creation intent without its own
    Allowed services takes its organization's default, so a pending project
    completes no looser than the organization allowed; an intent's own
    choice stays. The project network column goes
    too: each bundle folds its own network setting when it is opened. Tables
    a database lacks come whole from ``create_all``.
    """
    if "organizations" not in tables:
        return
    organization_columns = {
        column["name"] for column in sa.inspect(cx).get_columns("organizations")
    }
    if "allowed_services_default" not in organization_columns:
        cx.execute(
            sa.text(
                "ALTER TABLE organizations ADD COLUMN allowed_services_default VARCHAR"
            )
        )
    if "network_default" in organization_columns:
        cx.execute(
            sa.text(
                "UPDATE organizations SET allowed_services_default = CASE "
                "network_default WHEN 'off' THEN 'approved_services' "
                "WHEN 'on' THEN 'all_services' END "
                "WHERE allowed_services_default IS NULL"
            )
        )
        cx.execute(sa.text("ALTER TABLE organizations DROP COLUMN network_default"))
    if "project_creation_intents" in tables:
        cx.execute(
            sa.text(
                "UPDATE project_creation_intents SET allowed_services = ("
                "SELECT allowed_services_default FROM organizations "
                "WHERE organizations.id = project_creation_intents.organization_id"
                ") WHERE allowed_services IS NULL"
            )
        )
    project_columns = (
        {column["name"] for column in sa.inspect(cx).get_columns("projects")}
        if "projects" in tables
        else set()
    )
    if "network" in project_columns:
        cx.execute(sa.text("ALTER TABLE projects DROP COLUMN network"))


def _apply_upgrades_from(cx: sa.Connection, version: int, tables: set[str]) -> int:
    """Run each in-place upgrade from ``version`` on; return the version reached."""
    if version == 6:
        _apply_v6_to_v7(cx)
        version = 7
    if version == 7:
        _apply_v7_to_v8(cx)
        version = 8
    if version == 8:
        _apply_v8_to_v9(cx, tables)
        version = 9
    if version == 9:
        _apply_v9_to_v10(cx, set(sa.inspect(cx).get_table_names()))
        version = 10
    if version == 10:
        _apply_v10_to_v11(cx, set(sa.inspect(cx).get_table_names()))
        version = 11
    return version


def _upgrade_identity_schema_to_current(
    engine: sa.Engine, *, edition: str | None
) -> bool:
    """Apply supported in-place identity upgrades inside one serialized transaction.

    SQLite opens an explicit ``BEGIN IMMEDIATE`` write transaction before any
    DDL so multi-statement renames commit or roll back atomically. Non-SQLite
    dialects lock ``schema_marker`` with ``FOR UPDATE`` before inspecting
    ``schema_version`` or table structure.
    """
    with engine.begin() as cx:
        if cx.dialect.name == "sqlite":
            cx.exec_driver_sql("BEGIN IMMEDIATE")
        marker_query = sa.select(schema_marker)
        if cx.dialect.name != "sqlite":
            marker_query = marker_query.with_for_update()
        markers = cx.execute(marker_query).all()
        if (
            len(markers) != 1
            or markers[0].id != 1
            or (edition is not None and markers[0].edition != edition)
        ):
            return False
        version = markers[0].schema_version
        if version == SCHEMA_VERSION:
            return True
        if version not in (6, 7, 8, 9, 10):
            return False
        tables = set(sa.inspect(cx).get_table_names())
        if "users" not in tables:
            return False
        version = _apply_upgrades_from(cx, version, tables)
        cx.execute(
            schema_marker.update()
            .where(schema_marker.c.id == 1)
            .values(schema_version=version, stamped_at=datetime.now(UTC))
        )
        return version == SCHEMA_VERSION


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
            if version in (6, 7, 8, 9, 10) and _upgrade_identity_schema_to_current(
                engine, edition=edition
            ):
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
