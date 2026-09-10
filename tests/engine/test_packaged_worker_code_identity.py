"""RED-first acceptance for immutable packaged app/worker code identity."""

from __future__ import annotations

import json
import os
import re

import pytest

from tests.runtime_foundation_test_helpers import require_docker, require_ok, run


pytestmark = [pytest.mark.gap, pytest.mark.gap_env]


def _normalize_github_remote(remote: str) -> str | None:
    if remote.startswith("git@github.com:"):
        remote = "https://github.com/" + remote.removeprefix("git@github.com:")
    elif remote.startswith("ssh://git@github.com/"):
        remote = "https://github.com/" + remote.removeprefix("ssh://git@github.com/")
    if not remote.startswith("https://github.com/"):
        return None
    return remote.removesuffix(".git").rstrip("/")


def _repository_url() -> str:
    """Resolve this checkout's canonical GitHub repository URL.

    Development worktrees can use role-named remotes and have no remote
    literally named `origin`. A hard-coded `remote.origin.url` lookup then
    fails identity resolution regardless of what's actually checked out, so
    resolution must honor the workflow-provided repository identity and fall
    back across configured GitHub remotes.

    Preferred first: GITHUB_REPOSITORY, the `owner/repo` slug GitHub Actions
    sets automatically for the repository a workflow is running in. It is
    exactly what `docker/metadata-action` in .github/workflows/docker.yml
    derives the `org.opencontainers.image.source` label from (via
    `images: ghcr.io/${{ github.repository }}`), so honoring it here matches
    production label provenance precisely rather than approximating it.
    Falls back to the `cloud` remote, then to the first configured remote
    whose URL is a github.com repository — never weakening the final
    `https://github.com/...` shape assertion, only widening how it is found.
    """
    override = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if override:
        assert re.fullmatch(r"[^/\s]+/[^/\s]+", override), (
            f"GITHUB_REPOSITORY must be an owner/repo slug, got {override!r}"
        )
        return f"https://github.com/{override}"

    remote_names = require_ok(
        run(["git", "remote"]), what="git remote list"
    ).stdout.split()
    ordered = [name for name in ("cloud", "origin") if name in remote_names]
    ordered += [name for name in remote_names if name not in ordered]
    for name in ordered:
        proc = run(["git", "config", "--get", f"remote.{name}.url"])
        if proc.returncode != 0:
            continue
        normalized = _normalize_github_remote(proc.stdout.strip())
        if normalized is not None:
            return normalized

    assert False, (
        "identity gate requires an auditable GitHub repository origin: no "
        f"github.com remote found among {remote_names!r} and GITHUB_REPOSITORY "
        "is not set"
    )


def _designated_identity_image(slot: str) -> tuple[str, str, str]:
    image_env = f"FRISKET_IDENTITY_TEST_IMAGE_{slot}"
    sha_env = f"FRISKET_IDENTITY_TEST_SHA_{slot}"
    provenance_env = f"FRISKET_IDENTITY_TEST_PROVENANCE_{slot}"
    image = os.environ.get(image_env, "").strip()
    source_sha = os.environ.get(sha_env, "").strip()
    provenance = os.environ.get(provenance_env, "").strip()
    assert image and source_sha and provenance, (
        "the designated image-identity gate must preload two releases and set "
        f"{image_env}/{sha_env}/{provenance_env}; pytest never builds or pulls "
        "over an implicit network"
    )
    assert re.fullmatch(r"[0-9a-f]{40}", source_sha), (
        f"{sha_env} must be the full immutable 40-hex source SHA"
    )
    assert re.fullmatch(r"sha256:[0-9a-f]{64}|[^\s@]+@sha256:[0-9a-f]{64}", image), (
        f"{image_env} must be a preloaded immutable image id or digest, got {image!r}"
    )
    assert re.fullmatch(
        re.escape(_repository_url()) + r"/actions/runs/\d+(?:/attempts/\d+)?",
        provenance,
    ), f"{provenance_env} must identify this repository's trusted Actions build run"
    if slot == "A":
        head = require_ok(run(["git", "rev-parse", "HEAD"]), what="checkout HEAD")
        assert source_sha == head.stdout.strip(), (
            "identity image A is stale: its OCI revision must equal current checkout HEAD"
        )
    docker = require_docker()
    proc = run([docker, "image", "inspect", image], timeout=20)
    require_ok(proc, what=f"preloaded immutable identity image {slot}")
    inspected = json.loads(proc.stdout)[0]
    labels = inspected.get("Config", {}).get("Labels") or {}
    assert labels.get("org.opencontainers.image.revision") == source_sha
    assert labels.get("org.opencontainers.image.source") == _repository_url()
    return image, source_sha, str(inspected["Id"])


def _probe_image(tag: str) -> dict[str, object]:
    docker = require_docker()
    script = (
        "import json,tempfile; from pathlib import Path; "
        "from frisket.engine.worker_version import code_version; "
        "from frisket.engine.jobs.queue import SqliteJobQueue; "
        "from frisket.engine.jobs.worker import Worker; "
        "tmp=tempfile.TemporaryDirectory(); q=SqliteJobQueue(Path(tmp.name)/'q.db'); "
        "jid=q.enqueue('echo',{'packaged':True}); w=Worker(q,worker_id='packaged'); "
        "w.record_liveness(force=True); hb=q.list_worker_heartbeats()[0]; "
        "print(json.dumps({'code_version':code_version(),"
        "'enqueue_code_version':q.get(jid).code_version,"
        "'worker_version':w.worker_version,"
        "'heartbeat_worker_version':hb.worker_version,"
        "'git_present':Path('/app/.git').exists()})); q.close(); tmp.cleanup()"
    )
    proc = run(
        [
            docker,
            "run",
            "--pull=never",
            "--rm",
            "--entrypoint",
            "python",
            tag,
            "-c",
            script,
        ],
        timeout=120,
    )
    require_ok(proc, what=f"identity probe in {tag}")
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    assert lines, proc.stdout
    return json.loads(lines[-1])


def test_published_image_bakes_immutable_non_unknown_source_identity() -> None:
    image_a, release_a, image_id_a = _designated_identity_image("A")
    image_b, release_b, image_id_b = _designated_identity_image("B")
    print(
        json.dumps(
            {
                "identity_gate_image_a": image_id_a,
                "identity_gate_image_b": image_id_b,
                "source_sha_a": release_a,
                "source_sha_b": release_b,
            },
            sort_keys=True,
        )
    )
    assert release_a != release_b and image_id_a != image_id_b
    probe_a = _probe_image(image_a)
    probe_b = _probe_image(image_b)
    identity_fields = (
        "code_version",
        "enqueue_code_version",
        "worker_version",
        "heartbeat_worker_version",
    )
    assert {probe_a[field] for field in identity_fields} == {release_a}
    assert {probe_b[field] for field in identity_fields} == {release_b}
    assert release_a != "unknown" and release_b != "unknown"
    assert probe_a["git_present"] is False and probe_b["git_present"] is False
