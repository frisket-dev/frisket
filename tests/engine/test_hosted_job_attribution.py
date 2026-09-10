"""hosted-job-attribution-v1: on the hosted store, every job names an owner.

The incident: a user of org B POSTed ``/api/diagnostic-bundle`` and got back
org A's queue jobs, project slugs included. A downstream composition's
per-tenant filter read ``org_id`` alone and treated its absence as "matches
everyone" (fail open). That is fixed downstream. The base-side root cause is
why unattributable jobs existed at all: attribution was something each
enqueue author had to remember to put in a hand-built payload dict, and
``enqueue`` only checked it for jobs that happened to name a ``project_id``.

The two identities are deliberately NOT merged, because on a hosted tenant
sub-app they diverge (a downstream composition's tenant-app builder passes
``{"org_id": funding_account_id, "storage_org_id": storage_org_id}``):

* ``storage_org_id`` -- the DATA OWNER, the org whose directory the bundle
  lives in. This is what makes a ``project_id`` sensitive, so it is the
  tenancy label, and it is REQUIRED.
* ``org_id`` -- the FUNDING account, who pays and whose BYOK keys resolve.
  Engine-minted background work has no funding account in scope, so it is
  OPTIONAL -- but a claimed value must be well formed.

Local single-user and the self-hosted team server enqueue these same kinds
with no org at all; both declare ``hosted=False``, so none of this applies to
them. That posture gate is what makes the requirement safe to impose.
"""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa

from frisket.engine.jobs.queue import (
    MODEL_PULL_KIND,
    PROJECT_RUN_KIND,
    SERVER_SCOPED_JOB_PAYLOAD_KEY,
    SqliteJobQueue,
    jobs_table,
    normalize_queue_org_id,
)

SOURCE_POLL_KIND = "source.poll"


def _hosted(tmp_path, name="hosted.db"):
    return SqliteJobQueue(tmp_path / name, hosted=True)


def _local(tmp_path, name="local.db"):
    return SqliteJobQueue(tmp_path / name, hosted=False)


def _row(queue, job_id):
    with queue.engine.connect() as cx:
        return cx.execute(sa.select(jobs_table).where(jobs_table.c.id == job_id)).one()


# --------------------------------------------------------------------------
# The refusal: an unattributable hosted job cannot be minted at all.
# --------------------------------------------------------------------------


def test_hosted_job_with_no_owner_at_all_is_refused(tmp_path):
    """THE REPRODUCTION. Before the fence this enqueued happily and persisted a
    row with org_id NULL, storage_org_id NULL, project_id NULL -- a job no
    per-tenant surface can attribute, which is exactly what a filter has to
    guess about (and guessed wrong, in the incident's direction)."""
    queue = _hosted(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        queue.enqueue(MODEL_PULL_KIND, {"pull_id": 1, "workspace_root": str(tmp_path)})
    assert "require an owner" in str(excinfo.value)
    assert SERVER_SCOPED_JOB_PAYLOAD_KEY in str(excinfo.value), (
        "the refusal must name the knob that expresses the legitimate case, "
        "or an author whose job really is server-scoped has no way forward"
    )


def test_hosted_project_job_without_storage_identity_is_refused(tmp_path):
    """The pre-existing contract this fence was mirrored from, unchanged."""
    queue = _hosted(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        queue.enqueue(PROJECT_RUN_KIND, {"project_id": "victim"})
    assert "hosted project jobs require first-class storage_org_id identity" in str(
        excinfo.value
    )


@pytest.mark.parametrize("bad", [True, False, "not-a-number", 0, -1, 1.5])
def test_hosted_project_job_with_malformed_storage_identity_is_refused(tmp_path, bad):
    """Validated on the RAW payload value, so a bool/float/str can never
    normalize into tenant 1."""
    queue = _hosted(tmp_path)
    with pytest.raises(ValueError):
        queue.enqueue(PROJECT_RUN_KIND, {"project_id": "victim", "storage_org_id": bad})


def test_a_job_cannot_be_both_server_scoped_and_project_scoped(tmp_path):
    """The marker is a declaration of ownership, not an escape hatch from it:
    it must not become a way to smuggle a project job past the tenancy label."""
    queue = _hosted(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        queue.enqueue(
            PROJECT_RUN_KIND,
            {
                "project_id": "victim",
                "storage_org_id": 7,
                SERVER_SCOPED_JOB_PAYLOAD_KEY: True,
            },
        )
    assert "cannot be both server-scoped and project-scoped" in str(excinfo.value)


@pytest.mark.parametrize("bad", [True, "abc", 0, -3, 1.5, []])
def test_hosted_job_claiming_a_malformed_funding_identity_is_refused(tmp_path, bad):
    """org_id is optional, but a claimed one is validated at the MINT site.
    It used to be validated only at claim time, so a malformed funding identity
    was accepted here and terminal-failed a claim later, losing the work in
    between with nothing to point at."""
    queue = _hosted(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        queue.enqueue(
            PROJECT_RUN_KIND,
            {"project_id": "proj", "storage_org_id": 7, "org_id": bad},
        )
    assert "org_id" in str(excinfo.value)


# --------------------------------------------------------------------------
# Everything legitimate stays expressible.
# --------------------------------------------------------------------------


def test_server_scoped_job_is_expressible_on_the_hosted_queue(tmp_path):
    """model.pull fills the shared model cache under a workspace root; it is
    owned by the server, not by a tenant. That must remain sayable -- an
    explicit marker, not an implicit absence."""
    queue = _hosted(tmp_path)
    job_id = queue.enqueue(
        MODEL_PULL_KIND,
        {
            "pull_id": 1,
            "workspace_root": str(tmp_path),
            SERVER_SCOPED_JOB_PAYLOAD_KEY: True,
        },
    )
    row = _row(queue, job_id)
    assert row.project_id is None and row.storage_org_id is None
    assert json.loads(row.payload)[SERVER_SCOPED_JOB_PAYLOAD_KEY] is True


def test_engine_site_shape_storage_label_without_funding_account_is_accepted(tmp_path):
    """The shape every engine-minted background job emits (source polls, watch
    evaluations, digests, deliveries): a storage label and no org_id. Requiring
    org_id here would force the engine to invent a funding account it does not
    know -- and cloud attributes these by their storage label, which is why its
    filter accepts both spellings."""
    queue = _hosted(tmp_path)
    job_id = queue.enqueue(
        SOURCE_POLL_KIND,
        {
            "project_id": "news",
            "storage_org_id": 23,
            "source_id": 1,
            "workspace_root": str(tmp_path),
        },
    )
    row = _row(queue, job_id)
    assert row.storage_org_id == 23
    assert row.org_id is None


def test_tenant_scoped_non_project_job_is_accepted_without_the_marker(tmp_path):
    """A job that names a tenant but no bundle is already attributable; it does
    not need to declare itself server-scoped."""
    queue = _hosted(tmp_path)
    job_id = queue.enqueue(
        MODEL_PULL_KIND, {"pull_id": 1, "storage_org_id": 5, "workspace_root": "/w"}
    )
    assert _row(queue, job_id).storage_org_id == 5


def test_funding_account_may_differ_from_the_data_owner(tmp_path):
    """The hosted tenant sub-app's real shape: org_id is the funding account,
    storage_org_id is the data owner, and they are NOT the same org. A fence
    that required them to agree would break hosted dispatch outright."""
    queue = _hosted(tmp_path)
    job_id = queue.enqueue(
        PROJECT_RUN_KIND,
        {"project_id": "proj", "storage_org_id": 23, "org_id": 91},
    )
    row = _row(queue, job_id)
    assert row.storage_org_id == 23
    assert row.org_id == "91"


# --------------------------------------------------------------------------
# The constraint that decided the shape: local and team must not break.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"project_id": "proj"},
        {"project_id": "proj", "source_id": 1, "workspace_root": "/w"},
        {"pull_id": 1, "workspace_root": "/w"},
        {},
    ],
    ids=["project-run", "source-poll", "model-pull", "bare"],
)
def test_local_and_team_enqueue_the_same_jobs_with_no_org_at_all(tmp_path, payload):
    """Local single-user has no org concept; the self-hosted team server is
    single-org and passes only ``{"org_id": org_id}`` as an audit field, with
    no storage identity and no claimed ProjectStorageKey. BOTH declare
    hosted=False (team/app.py is explicit about it), so the attribution
    requirement is structurally out of reach for them."""
    queue = _local(tmp_path)
    assert queue.enqueue(PROJECT_RUN_KIND, dict(payload)) > 0


def test_team_audit_org_id_still_enqueues_without_a_storage_identity(tmp_path):
    """team/app.py spreads queue_payload_extra={"org_id": org_id} and nothing
    else. On its hosted=False queue that stays a pure audit field."""
    queue = _local(tmp_path, "team.db")
    job_id = queue.enqueue(PROJECT_RUN_KIND, {"project_id": "proj", "org_id": 4})
    row = _row(queue, job_id)
    assert row.org_id == "4" and row.storage_org_id is None


# --------------------------------------------------------------------------
# One definition of a queue org identity, applied at both ends.
# --------------------------------------------------------------------------


def test_normalize_queue_org_id_is_the_one_definition_the_worker_also_uses():
    """The worker re-validates the persisted column before resolving
    credentials against it. Enqueue and claim must not be able to disagree
    about what a well-formed funding identity is."""
    from frisket.engine.jobs import worker

    assert worker._normalize_queue_org_id is normalize_queue_org_id
