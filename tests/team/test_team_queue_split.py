"""Wave-2 blocker regression: app + standalone worker must share one queue.

Root cause:
``create_team_app`` called ``create_app`` without a ``queue=``, so
``Workspace`` fell back to a local ``<data_dir>/.queue.db`` SQLite file
(``server/workspace.py``'s ``open_queue(workspace=self.root)`` default). The
standalone self-host worker (``frisket worker``/docker-compose's ``worker``
service) instead opens ``FRISKET_RUN_QUEUE_DATABASE_URL`` (Postgres in
production). Two different queues: every self-hosted background job (OCR,
transcribe, recipes, digests, watches) sat in the app's SQLite file forever,
never claimed.

This module proves, per the review's non-negotiable acceptance bar:

* a job enqueued through the team app's ACTUAL HTTP composition is claimed
  and completed by an INDEPENDENTLY CONNECTED standalone worker: a separate
  ``JobQueue``/SQLAlchemy engine, opened only from the run-queue URL via
  ``frisket.cli.worker(["--database-url", ...])`` -- the same call
  ``_run_worker`` makes -- sharing no Python queue/handle object with the
  app (``TestQueueSplitEndToEnd``). This runs in the SAME pytest
  interpreter, not across an OS process/container boundary: there is no
  ``subprocess``/docker step here, so it does not cover env bootstrap,
  provisioned runtime-role permissions plus strict schema mode, BYOK secret
  decryption, or the literal Compose worker entrypoint. What it DOES prove
  is real: the durable run/job-status assertions below only pass because the
  worker reconnects to the SAME underlying database the app enqueued into --
  under the original bug (app SQLite / worker Postgres) they would fail,
  because the worker's independently-opened queue would simply never see
  the job;
* the wiring itself: ``Workspace.queue`` resolves to the configured
  run-queue URL, never the local SQLite fallback, when one is configured
  (``TestQueueWiringMatchesStandaloneWorker``);
* the team app REFUSES a missing run-queue locator rather than silently
  falling back to an undrainable local queue; local single-node is a
  different entry point and is unaffected
  (``TestTeamAppRequiresRunQueueLocator``);
* the related MEDIUM finding -- the no-arg worker's scheduler-root discovery
  only knowing the legacy ``FRISKET_DATA_DIR/projects/<org_id>`` layout, not
  the team app's actual flat ``FRISKET_DATA_DIR/<slug>.frisket`` layout, so
  scheduled digests/watches never started -- is fixed, including the two
  storage-identity edge cases exposed in the first
  version of that fix (a numeric-basename flat data dir misread as an org;
  a stray non-numeric ``projects/`` subdirectory suppressing the flat
  fallback) (``TestSchedulerRootLayoutMatchesTeamApp``).

The primary end-to-end test uses a URL-addressed SQLite run-queue
(``sqlite:///...``) rather than live Postgres so it runs unconditionally:
``open_queue(database_url=...)`` is backend-agnostic (the ``database_url``
branch, not the local-workspace branch), so a SQLite URL exercises the exact
same wiring/branch Postgres does in production. A second, real-Postgres
variant is included gated on ``FRISKET_PG_TEST_URL`` (same convention as
``tests/test_job_queue.py``'s ``pg_queue`` fixture) for anyone who wants the
literal production backend; it is skipped when unset, as it is in this
worktree. To run it: start a scratch Postgres and
``FRISKET_PG_TEST_URL=postgresql+psycopg://user:pass@host/db uv run pytest
tests/test_team_queue_split.py -q``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from frisket import cli
from frisket.engine.jobs import SOURCE_POLL_KIND, open_queue
from frisket.engine.jobs.queue import QUEUE_DB_NAME, SqlAlchemyJobQueue
from frisket.engine.store import Project
from frisket.engine.store.sources import SourceStore
from frisket.team.app import TeamConfig, create_team_app
from tests.http_test_helpers import v1_action_from_canonical_run_spec
from tests.team_setup_helpers import claim_server, sign_in_with_magic_link

PG_URL = os.environ.get("FRISKET_PG_TEST_URL")


async def _mail(_email: str, _link: str) -> bool:
    return True


def _config(tmp_path: Path, **overrides: Any) -> TeamConfig:
    values: dict[str, Any] = dict(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        data_dir=tmp_path / "data",
        base_url="http://testserver",
        organization_name="Self-Host Desk",
        admin_emails={"owner@example.com"},
    )
    values.update(overrides)
    return TeamConfig(**values)


def _login(client: TestClient, app: Any) -> None:
    sign_in_with_magic_link(app, client, "owner@example.com")


def _project_with_queued_action(client: TestClient) -> tuple[str, dict[str, Any]]:
    """A QUEUED_PROJECT_RUN action needing no model/provider credential.

    ``map.python`` is deterministic, zero-cost, local (cost_policy
    kind="none") and still declared in
    ``action_specs._QUEUED_PROJECT_RUN_KINDS``, so it genuinely goes through
    the queue like ``map.classify``/OCR/transcribe do, without pulling in
    provider-key/BYOK setup that is orthogonal to the queue-split bug.
    """
    made = client.post("/api/projects", json={"name": "Queue Split Evidence"})
    assert made.status_code == 200, made.text
    pid = made.json()["id"]
    imported = client.post(
        f"/api/projects/{pid}/import/csv",
        files={
            "file": (
                "notes.csv",
                b'note\n"call 555-123-4567 for details"\n',
                "text/csv",
            )
        },
    )
    assert imported.status_code == 200, imported.text
    sheet_id = int(imported.json()["sheet_id"])
    action = v1_action_from_canonical_run_spec(
        {
            "action_kind": "map.python",
            "sheet_id": sheet_id,
            "input_columns": ["note"],
            "code": "result = row['note']",
            "output_name": "copied_note",
        },
        idempotency_key="queue-split-python-1",
    )
    return pid, action


class TestTeamAppRequiresRunQueueLocator:
    """Regression: create_team_app must REFUSE a missing run-queue locator
    rather than silently fall back to a workspace-local queue no standalone
    worker can drain. (Local single-node use is a different entry point —
    frisket <workspace> / create_app — and is unaffected.)"""

    def test_no_run_queue_configured_is_rejected_not_silently_split(
        self, tmp_path: Path
    ) -> None:
        """Merge note (local-models-phase-2-3 x reconciled main): both sides
        fixed the app/worker queue split; the branch additionally made the
        locator MANDATORY (the queue-composition audit, 'general run-queue mismatch') —
        a workspace-local SQLite fallback is exactly the topology a
        standalone `frisket worker` can never drain, so `create_team_app`
        now raises instead of silently falling back. This pin previously
        asserted the fallback; it now asserts the refusal."""
        config = _config(tmp_path)
        assert config.run_queue_database_url is None
        with pytest.raises(ValueError, match="FRISKET_RUN_QUEUE_DATABASE_URL"):
            create_team_app(config, send_magic_email=_mail)


class TestQueueWiringMatchesStandaloneWorker:
    """Focused proof of the composition wiring itself (fast, no HTTP/worker)."""

    def test_run_queue_database_url_threads_into_workspace_queue(
        self, tmp_path: Path
    ) -> None:
        run_queue_url = f"sqlite:///{tmp_path / 'run-queue.db'}"
        config = _config(tmp_path, run_queue_database_url=run_queue_url)
        app = create_team_app(config, send_magic_email=_mail)
        queue = app.state.workspace.queue
        assert isinstance(queue, SqlAlchemyJobQueue)
        assert str(queue.engine.url) == run_queue_url
        # Never the multi-tenant hosted posture (queue-hosted-
        # posture-explicit-v1): the open team server has no claimed
        # ProjectStorageKey routing and never populates storage_org_id.
        assert queue._hosted_storage is False  # noqa: SLF001
        # And never falls back to the local per-workspace .queue.db file.
        assert not (config.data_dir / QUEUE_DB_NAME).exists()


def _standalone_worker_claims_and_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_queue_url: str,
    data_dir: Path,
) -> None:
    """Enqueue via the team app's real HTTP composition; complete via an
    independently CONNECTED standalone worker -- ``frisket.cli.worker`` with
    ``--database-url``/no ``--workspace`` (hosted=False), the same call shape
    docker-compose's ``worker`` service invokes via
    FRISKET_RUN_QUEUE_DATABASE_URL. This runs the worker in-process (same
    pytest interpreter, no OS subprocess/container boundary), but it opens
    its OWN JobQueue/SQLAlchemy engine from the URL alone, sharing no queue
    object with the app -- so the durable status assertions below only pass
    if that independently-opened engine reconnects to the SAME database the
    app enqueued into. It does not cover env bootstrap, provisioned
    runtime-role permissions plus strict schema mode, BYOK secret
    decryption, or the literal Compose entrypoint/container boundary."""
    config = _config(tmp_path, data_dir=data_dir, run_queue_database_url=run_queue_url)
    app = create_team_app(config, send_magic_email=_mail)
    claim_server(app, origin=config.base_url)
    client = TestClient(app)
    _login(client, app)
    pid, action = _project_with_queued_action(client)

    queued = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)
    assert queued.status_code == 200, queued.text
    body = queued.json()
    assert body["status"] in {"queued", "running"}, body
    run_id = int(body["run_id"])

    # The app really did write to the CONFIGURED run-queue, not a local
    # fallback: read it back through the app's own Workspace.queue handle.
    app_side_job = app.state.workspace.queue.get_project_run_job(pid, run_id)
    assert app_side_job is not None
    assert app_side_job.status == "queued"

    # The standalone worker: a fresh JobQueue/Worker built ONLY from the
    # run-queue URL + FRISKET_DATA_DIR, sharing no Python queue/handle
    # object with the app -- the CLI call shape a separate `frisket worker`
    # container runs (same interpreter here, not a separate OS process).
    monkeypatch.setenv("FRISKET_DATA_DIR", str(data_dir))
    monkeypatch.delenv("FRISKET_RUN_QUEUE_DATABASE_URL", raising=False)
    monkeypatch.delenv("FRISKET_DATABASE_URL", raising=False)
    rc = cli.worker(
        [
            "--database-url",
            run_queue_url,
            "--drain",
            "--schedule-sources-interval",
            "0",
        ]
    )
    assert rc == 0

    status = client.get(f"/api/projects/{pid}/actions/runs/{run_id}/status")
    assert status.status_code == 200, status.text
    assert status.json()["run"]["status"] == "completed", status.text

    finished_job = app.state.workspace.queue.get_project_run_job(pid, run_id)
    assert finished_job is not None
    assert finished_job.status == "done", finished_job.error


class TestQueueSplitEndToEnd:
    def test_team_app_enqueue_claimed_and_completed_by_standalone_worker_sqlite_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run_queue_url = f"sqlite:///{tmp_path / 'run-queue.db'}"
        _standalone_worker_claims_and_completes(
            tmp_path,
            monkeypatch,
            run_queue_url=run_queue_url,
            data_dir=tmp_path / "data",
        )

    @pytest.mark.skipif(not PG_URL, reason="FRISKET_PG_TEST_URL not set")
    def test_team_app_enqueue_claimed_and_completed_by_standalone_worker_postgres(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert PG_URL is not None
        engine = sa.create_engine(PG_URL, future=True)
        with engine.begin() as cx:
            cx.execute(sa.text("DROP TABLE IF EXISTS jobs CASCADE"))
            cx.execute(sa.text("DROP TABLE IF EXISTS worker_heartbeats CASCADE"))
            cx.execute(sa.text("DROP TABLE IF EXISTS frisket_queue_migrations CASCADE"))
        engine.dispose()
        _standalone_worker_claims_and_completes(
            tmp_path,
            monkeypatch,
            run_queue_url=PG_URL,
            data_dir=tmp_path / "data",
        )


class TestSchedulerRootLayoutMatchesTeamApp:
    """Scheduled source polls, digests, and watches must start for the
    team app's actual (flat) bundle layout, not only the legacy per-org
    FRISKET_DATA_DIR/projects/<org_id> shape."""

    def test_no_arg_scheduler_roots_fall_back_to_flat_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from frisket.cli import _source_scheduler_roots

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.delenv("FRISKET_SOURCE_WORKSPACE_ROOTS", raising=False)
        monkeypatch.setenv("FRISKET_DATA_DIR", str(data_dir))

        # No projects/<org_id> subdirectories exist (team app's real
        # layout) -- the flat data dir itself must be the discovered root,
        # paired with storage_org_id=None: a flat layout has no claimed
        # per-org storage identity at all.
        assert _source_scheduler_roots(None) == [(data_dir, None)]

        # Once a legacy per-org layout DOES exist, it still wins (unchanged
        # behavior for the true multi-tenant hosted shape), paired with the
        # real parsed org id.
        org_root = data_dir / "projects" / "7"
        org_root.mkdir(parents=True)
        assert _source_scheduler_roots(None) == [(org_root, 7)]

    def test_flat_data_dir_with_numeric_basename_is_not_misread_as_an_org(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A flat FRISKET_DATA_DIR whose basename
        happens to be a plain integer (e.g. /mnt/7) must NOT be misclassified
        as hosted storage org 7 -- storage_org_id must stay None for the flat
        fallback regardless of the data dir's own name. Getting this wrong
        makes claimed_project_location build the wrong path
        (<data_dir>/projects/7/<slug>.frisket instead of
        <data_dir>/<slug>.frisket) and the scheduled job fails/retries."""
        from frisket.cli import _source_scheduler_roots

        numeric_data_dir = tmp_path / "7"
        numeric_data_dir.mkdir()
        monkeypatch.delenv("FRISKET_SOURCE_WORKSPACE_ROOTS", raising=False)
        monkeypatch.setenv("FRISKET_DATA_DIR", str(numeric_data_dir))

        roots = _source_scheduler_roots(None)
        assert roots == [(numeric_data_dir, None)]

    def test_stray_nonnumeric_projects_dir_does_not_suppress_flat_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_hosted_project_roots must not treat
        every directory under <data_dir>/projects as a hosted org root. A
        stray non-numeric directory there (a cache dir, a leftover) beside
        real flat <data_dir>/<slug>.frisket bundles must not suppress the
        flat fallback and misroute discovery into the (bogus) hosted
        per-org layout."""
        from frisket.cli import _source_scheduler_roots

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "projects").mkdir()
        (data_dir / "projects" / "cache").mkdir()  # non-numeric, not an org
        monkeypatch.delenv("FRISKET_SOURCE_WORKSPACE_ROOTS", raising=False)
        monkeypatch.setenv("FRISKET_DATA_DIR", str(data_dir))

        # No REAL (numeric) per-org root exists, so the flat data dir itself
        # is still the discovered root -- the stray projects/cache/ dir must
        # not have been counted as proof of a hosted layout.
        assert _source_scheduler_roots(None) == [(data_dir, None)]

    def test_standalone_worker_schedules_due_source_under_flat_team_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        from frisket import ingest

        monkeypatch.setattr(ingest, "url_is_safe", lambda url: True)
        feed = (
            b"<?xml version='1.0'?><rss version='2.0'><channel>"
            b"<title>Flat Team Feed</title><item><guid>a</guid><title>A</title>"
            b"<link>https://example.com/a</link></item></channel></rss>"
        )

        class FakeResponse:
            is_redirect = False
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def iter_bytes(self):
                yield feed

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def stream(self, method, url):
                assert method == "GET"
                assert url == "https://example.com/feed.xml"
                return FakeResponse()

        monkeypatch.setattr(httpx, "Client", FakeClient)

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        # The team app's real bundle layout: <data_dir>/<slug>.frisket
        # directly, no projects/ nesting at all.
        p = Project.create(data_dir / "flat-team.frisket", name="flat-team")
        sid = SourceStore(p).add_source(
            name="Flat Feed",
            kind="rss",
            url="https://example.com/feed.xml",
            schedule="@hourly",
        )
        p.close()
        assert not (data_dir / "projects").exists()

        run_queue_url = f"sqlite:///{tmp_path / 'run-queue.db'}"
        q = open_queue(database_url=run_queue_url)
        monkeypatch.setenv("FRISKET_DATA_DIR", str(data_dir))
        monkeypatch.delenv("FRISKET_RUN_QUEUE_DATABASE_URL", raising=False)
        monkeypatch.delenv("FRISKET_SOURCE_WORKSPACE_ROOTS", raising=False)

        assert cli.worker(["--database-url", run_queue_url, "--drain"]) == 0

        jobs = q.list_jobs(status="done")
        assert len([job for job in jobs if job.kind == SOURCE_POLL_KIND]) == 1
        p2 = Project(data_dir / "flat-team.frisket")
        assert SourceStore(p2).source_runs(sid)[0]["status"] == "ok"
        p2.close()
        q.close()
