"""Red-first pin: ``frisket.team.team_bootstrap``'s queue-table allowlist
must not depend on import order.

``frisket.jobs.model_pull_store`` registers its ``model_pulls`` table onto
the queue's shared ``jobs_metadata`` ``sa.MetaData`` as a side effect of
being imported. ``team_bootstrap``'s clean-cut check treats every table on
``jobs_metadata`` as a legitimate co-tenant of a team control-plane database
that shares its run queue -- but only if ``model_pull_store`` has already
been imported by SOMETHING in this process by the time that allowlist is
computed. ``frisket.team.app`` happens to import ``operational_routes``
(which imports ``model_pull_store``) before it imports ``team_bootstrap``,
so this bug never showed up through that one call path -- but a fresh
interpreter that imports ``frisket.team.team_bootstrap`` directly (or any
other caller that reaches it before anything else has pulled in
``model_pull_store``, e.g. a separately-started run-queue process having
already created ``model_pulls`` on a shared database) sees the pull-store
table as "unexpected" and refuses to boot a perfectly valid team deployment.

Every test here runs the check in a genuinely fresh interpreter subprocess:
importing ``frisket.jobs.model_pull_store`` anywhere else first in the same
pytest process (virtually guaranteed -- other test modules import it) would
silently mask the bug.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def _run(snippet: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(snippet)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_team_bootstrap_import_alone_registers_model_pulls_in_the_allowlist() -> None:
    """Importing ONLY ``frisket.team.team_bootstrap`` -- never
    ``frisket.jobs.model_pull_store``, ``frisket.team.app``, or
    ``frisket.team.operational_routes`` -- must still see ``model_pulls`` in
    its own queue-table allowlist. Otherwise the allowlist's completeness
    depends on which module some OTHER caller happened to import first."""
    _run(
        """
        import sys
        assert "frisket.engine.jobs.model_pull_store" not in sys.modules

        import frisket.team.team_bootstrap as team_bootstrap
        from frisket.engine.jobs.queue import jobs_metadata

        assert "model_pulls" in jobs_metadata.tables, (
            "importing frisket.team.team_bootstrap alone did not register "
            "model_pulls onto jobs_metadata -- the allowlist is "
            "import-order-dependent"
        )
        """
    )


def test_team_bootstrap_accepts_a_database_carrying_model_pulls_created_by_another_process(
    tmp_path: Path,
) -> None:
    """Exact repro of the review's scenario: a SEPARATE process (the run
    queue / a worker) already created ``model_pulls`` on the shared database
    before this process -- the team app -- ever started. This process's
    first frisket import is ``frisket.team.team_bootstrap`` itself (never
    ``model_pull_store`` directly). With the import-order bug, the clean-cut
    check's allowlist snapshot would not yet know about ``model_pulls`` (this
    process never independently imported ``model_pull_store``), so it would
    treat the pre-existing table as an unexpected schema table and raise
    ``PreSplitSchemaError`` instead of booting."""
    db_path = tmp_path / "shared.db"
    _run(
        f"""
        import sys
        assert "frisket.engine.jobs.model_pull_store" not in sys.modules

        import sqlalchemy as sa

        # Simulate the run queue's own provisioning, done by a different
        # process, before this one ever imports anything frisket-related.
        engine = sa.create_engine("sqlite:///{db_path}", future=True)
        with engine.begin() as cx:
            cx.execute(sa.text("CREATE TABLE jobs (id INTEGER PRIMARY KEY)"))
            cx.execute(
                sa.text(
                    "CREATE TABLE worker_heartbeats (worker_id TEXT PRIMARY KEY)"
                )
            )
            cx.execute(sa.text("CREATE TABLE model_pulls (id INTEGER PRIMARY KEY)"))

        from frisket.team.team_bootstrap import initialize_team_schema_and_org

        initialize_team_schema_and_org(engine, organization_name="acme")

        with engine.connect() as cx2:
            tables = set(sa.inspect(cx2).get_table_names())
        assert "orgs" in tables
        assert "model_pulls" in tables
        """
    )
