"""Process-safe fresh bootstrap for the single-organization team edition."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import sqlalchemy as sa
from filelock import FileLock

from frisket.engine.jobs.queue_schema import queue_table_names
from frisket.team.bootstrap import PreSplitSchemaError, SCHEMA_VERSION, schema_marker
from frisket.team.schema import metadata, organizations


def _queue_tables() -> set[str]:
    # A team database that also hosts the run queue legitimately carries the
    # queue's own tables; they are an expected co-tenant, not an unexpected
    # table that should fail the clean-cut check.
    return queue_table_names()


def _validate_existing_tables(cx: sa.Connection, tables: set[str]) -> None:
    # Queue tables are a legitimate co-tenant in a team database that shares
    # its run queue with its control-plane schema -- filtered out entirely
    # before this function's clean-cut check, which only ever describes
    # `frisket.team.schema`'s own tables (see `_queue_tables` above). A
    # database pointed at by a separate `FRISKET_RUN_QUEUE_DATABASE_URL`
    # never has these tables at all, so this filter is a no-op there.
    team_tables = tables - _queue_tables()
    if not team_tables:
        return
    if schema_marker.name not in team_tables:
        raise PreSplitSchemaError(
            "unstamped control-plane schema; clean cut has no migration: drop and reseed"
        )
    expected = set(metadata.tables) | {schema_marker.name}
    if team_tables != expected:
        raise PreSplitSchemaError(
            f"team control plane has unexpected schema tables "
            f"{sorted(team_tables - expected)}; drop and reseed"
        )
    markers = cx.execute(sa.select(schema_marker)).all()
    if len(markers) != 1 or markers[0].id != 1:
        raise PreSplitSchemaError("schema marker singleton is invalid; drop and reseed")
    marker = markers[0]
    if marker.edition != "team" or marker.schema_version != SCHEMA_VERSION:
        raise PreSplitSchemaError(
            f"team requires marker edition='team' version={SCHEMA_VERSION}; "
            f"got edition={marker.edition!r} version={marker.schema_version!r}; "
            "drop and reseed"
        )


@contextmanager
def _sqlite_file_lock(engine: sa.Engine) -> Iterator[None]:
    database = engine.url.database
    if engine.dialect.name != "sqlite" or not database or database == ":memory:":
        yield
        return
    # Share the queue initializer's lock: team and queue schemas can occupy
    # the same SQLite file, so separate locks would still permit concurrent
    # reflection and DDL from independent app boots.
    path = Path(f"{Path(database).expanduser().resolve()}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path), timeout=-1):
        yield


def initialize_team_schema_and_organization(
    engine: sa.Engine, *, organization_name: str
) -> int:
    """Initialize/validate under a process+database single-writer lock."""
    with _sqlite_file_lock(engine), engine.begin() as cx:
        if cx.dialect.name == "postgresql":
            cx.execute(sa.text("SELECT pg_advisory_xact_lock(719238411)"))
        existing = set(sa.inspect(cx).get_table_names())
        _validate_existing_tables(cx, existing)
        metadata.create_all(cx)
        schema_marker.metadata.create_all(cx)
        values = {
            "id": 1,
            "edition": "team",
            "schema_version": SCHEMA_VERSION,
        }
        if cx.dialect.name == "sqlite":
            cx.execute(schema_marker.insert().prefix_with("OR IGNORE").values(**values))
        elif cx.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert

            cx.execute(insert(schema_marker).values(**values).on_conflict_do_nothing())
        else:
            if cx.execute(sa.select(schema_marker.c.id)).scalar_one_or_none() is None:
                cx.execute(schema_marker.insert().values(**values))

        tables = set(sa.inspect(cx).get_table_names())
        _validate_existing_tables(cx, tables)
        rows = (
            cx.execute(sa.select(organizations.c.id).order_by(organizations.c.id))
            .scalars()
            .all()
        )
        if rows and rows != [1]:
            raise PreSplitSchemaError(
                "team organization singleton is invalid; drop and reseed"
            )
        if not rows:
            cx.execute(
                organizations.insert().values(
                    id=1,
                    name=organization_name,
                    display_name=organization_name,
                )
            )
        return 1


def preflight_team_schema(engine: sa.Engine) -> None:
    """Read-only clean-cut validation under the cross-process bootstrap lock."""
    with _sqlite_file_lock(engine), engine.begin() as cx:
        if cx.dialect.name == "postgresql":
            cx.execute(sa.text("SELECT pg_advisory_xact_lock(719238411)"))
        _validate_existing_tables(cx, set(sa.inspect(cx).get_table_names()))


__all__ = ["initialize_team_schema_and_organization", "preflight_team_schema"]
