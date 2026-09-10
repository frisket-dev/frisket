"""Process-safe fresh bootstrap for the single-organization team edition."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import sqlalchemy as sa
from filelock import FileLock

# `frisket.jobs.model_pull_store` registers its `model_pulls` table onto the
# SAME `jobs_metadata` `sa.MetaData` as a module-level side effect of being
# imported -- `jobs_metadata.tables` only contains `model_pulls` once that
# module has been imported at least once in this process. `frisket.team.app`
# happens to import `operational_routes` (which imports `model_pull_store`)
# before it imports this module, so the snapshot below is always complete
# THROUGH THAT PATH -- but any other caller that imports
# `frisket.team.team_bootstrap` first (a fresh interpreter, a direct `import
# frisket.team.team_bootstrap`, a different composition/test) would take the
# snapshot before `model_pulls` exists, and then treat a shared-database
# team deployment's own `model_pulls` table as an "unexpected schema table"
# and refuse to boot. Import every `jobs_metadata` contributor explicitly,
# right here, before computing the allowlist -- this module does not depend
# on what some OTHER module happened to import first.
from frisket.engine.jobs import model_pull_store as _model_pull_store  # noqa: F401
from frisket.engine.jobs.queue import jobs_metadata
from frisket.engine.jobs.queue_migrations import QUEUE_SCHEMA_LEDGER
from frisket.team.bootstrap import PreSplitSchemaError, SCHEMA_VERSION, schema_marker
from frisket.team.schema import metadata, orgs


def _queue_tables() -> set[str]:
    # The team composition's run queue is now the SAME
    # database as its control-plane schema (`create_team_app`'s
    # `open_queue(database_url=...)`), so a team control-plane database
    # legitimately carries the run-queue's own tables (`jobs`,
    # `worker_heartbeats`, `model_pulls`, the schema-migration ledger)
    # alongside the team schema's tables -- these are a known, expected
    # co-tenant, not an "unexpected table" that should fail this clean-cut
    # check. A database pointed at by a SEPARATE
    # `FRISKET_RUN_QUEUE_DATABASE_URL` never has these at all, so this
    # addition is a no-op for that (also still-supported) shape.
    #
    # Computed lazily (not a module-level constant) as defense in depth on
    # top of the explicit import above: any FUTURE `jobs_metadata` contributor
    # is picked up the moment it has been imported by anyone, anywhere, by
    # the time this function actually runs, rather than only by whatever set
    # of modules happened to be imported at THIS module's own import time.
    return set(jobs_metadata.tables) | {QUEUE_SCHEMA_LEDGER}


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
    path = (
        Path(database).resolve().with_suffix(Path(database).suffix + ".team-init.lock")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path), timeout=-1):
        yield


def initialize_team_schema_and_org(engine: sa.Engine, *, organization_name: str) -> int:
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
        rows = cx.execute(sa.select(orgs.c.id).order_by(orgs.c.id)).scalars().all()
        if rows and rows != [1]:
            raise PreSplitSchemaError(
                "team organization singleton is invalid; drop and reseed"
            )
        if not rows:
            cx.execute(
                orgs.insert().values(
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


__all__ = ["initialize_team_schema_and_org", "preflight_team_schema"]
