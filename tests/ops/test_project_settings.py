"""Typed project settings and the media host-policy toggle they carry.

The settings document is stored as one validated JSON blob under a ``meta``
key. These cover the two directions that matter: a bad document must never
make a project unopenable, and a bad *write* must not be silently accepted.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from frisket.server.app import create_app

from frisket.project_settings import (
    PROJECT_SETTINGS_META_KEY,
    ProjectSettings,
    patch_project_settings,
    read_project_settings,
    write_project_settings,
)


class _FakeProject:
    """The two ``meta`` accessors the settings module needs."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.meta: dict[str, str] = dict(initial or {})

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        return self.meta.get(key, default)

    def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value


def test_private_hosts_default_to_inherit_and_resolve_to_allowed(monkeypatch):
    from frisket.ops.egress_policy import MEDIA_PRIVATE_HOSTS_ENV, media_egress_policy

    # The stored default is "inherit the server default"; on a local install
    # with no server floor that resolves to allowed — a media host on the
    # operator's own network is a legitimate target, and tightening on
    # upgrade would break it silently.
    monkeypatch.delenv(MEDIA_PRIVATE_HOSTS_ENV, raising=False)
    assert ProjectSettings().media_allow_private_hosts is None
    assert read_project_settings(_FakeProject()).media_allow_private_hosts is None
    assert media_egress_policy(_FakeProject()).allow_private_hosts is True


def test_patch_round_trips_through_the_meta_document():
    project = _FakeProject()
    patch_project_settings(project, {"media_allow_private_hosts": False})

    assert read_project_settings(project).media_allow_private_hosts is False
    stored = json.loads(project.meta[PROJECT_SETTINGS_META_KEY])
    assert stored["media_allow_private_hosts"] is False


def test_patch_leaves_unmentioned_settings_alone():
    project = _FakeProject()
    write_project_settings(project, ProjectSettings(media_allow_private_hosts=False))

    patch_project_settings(project, {})

    assert read_project_settings(project).media_allow_private_hosts is False


def test_patch_rejects_an_unknown_key():
    project = _FakeProject()
    # A typo must fail loudly rather than be stored and ignored forever.
    with pytest.raises(ValidationError):
        patch_project_settings(project, {"media_allow_privat_hosts": False})


@pytest.mark.parametrize(
    "stored",
    ["{ not json", "null", '"a string"', "[1, 2]", '{"media_allow_private_hosts": 7}'],
)
def test_unreadable_documents_fall_back_to_defaults(stored: str):
    project = _FakeProject({PROJECT_SETTINGS_META_KEY: stored})
    # A corrupt settings blob must not make the project unopenable.
    assert read_project_settings(project).media_allow_private_hosts is None


def test_read_tolerates_keys_a_newer_build_wrote():
    project = _FakeProject(
        {
            PROJECT_SETTINGS_META_KEY: json.dumps(
                {"media_allow_private_hosts": False, "setting_from_the_future": "x"}
            )
        }
    )

    assert read_project_settings(project).media_allow_private_hosts is False


def test_download_media_honors_the_policy_argument():
    from frisket.ops.egress_policy import STRICT_POLICY
    from frisket.ops.ytdlp import download_media

    with pytest.raises(ValueError, match="private/loopback/metadata"):
        download_media(
            "http://127.0.0.1:8080/video",
            extractor=lambda *_a, **_k: {},
            policy=STRICT_POLICY,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("media_downloader", [False, True])
async def test_fetch_url_threads_the_project_setting_into_each_download(
    monkeypatch, media_downloader
):
    from frisket.actions.types import RowError
    from frisket.engine.executor import file_fetch
    from frisket.engine.executor.row_file_stage import RowFileStager

    project = _FakeProject()
    write_project_settings(project, ProjectSettings(media_allow_private_hosts=False))
    seen: dict[str, Any] = {}

    def _fake_download(url: str, **kwargs: Any):
        seen["url"] = url
        seen["policy"] = kwargs.get("policy")
        raise RuntimeError("stop after argument capture")

    monkeypatch.setattr(
        "frisket.authoring.workbench.plugin_runtime_capabilities.enabled_workbench_plugin_ids",
        lambda _project: set(),
    )
    monkeypatch.setattr(
        file_fetch, "is_supported_ytdlp_media_url", lambda *_a, **_kw: media_downloader
    )
    monkeypatch.setattr(file_fetch.ytdlp, "download_media", _fake_download)
    monkeypatch.setattr(file_fetch.enclosures, "download_url", _fake_download)
    stager = RowFileStager(project)
    fetcher = file_fetch.AdmittedFileFetcher(project, stager)
    try:
        bound = fetcher.bind_row(None, sheet_id=1, row_id=1, sources={})
        with pytest.raises(RowError, match="URL download failed"):
            await bound.fetch("https://example.com/file.mp3")
    finally:
        await fetcher.aclose()
        stager.close()

    # Wiring the setting is the whole feature; a default-shaped call here would
    # mean the toggle silently does nothing.
    assert seen["policy"].allow_private_hosts is False


def test_download_url_honors_the_policy():
    """The documented behavior ('fetch media from a server on your own
    network') on the direct path: a loopback URL is refused by default and
    fetched when the policy allows private hosts."""
    import http.server

    from frisket.ops import enclosures
    from frisket.ops.egress_policy import MediaEgressPolicy
    from tests.deterministic_time import controlled_time

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server API
            body = b"media-bytes"
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: A002
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    with controlled_time() as t:
        t.background(server.serve_forever)
        try:
            url = f"http://127.0.0.1:{server.server_port}/a.mp3"
            _data, _mime, _filename, err = enclosures.download_url(url)
            assert err is not None and "blocked URL" in err

            data, mime, _filename, err = enclosures.download_url(
                url, policy=MediaEgressPolicy(allow_private_hosts=True)
            )
            assert err is None
            assert data == b"media-bytes"
            assert mime == "audio/mpeg"
        finally:
            server.shutdown()


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def _settings_project(tmp_path):
    # Built inline rather than via conftest.make_client: the repo-root
    # conftest.py shadows tests/conftest.py under the module name `conftest`,
    # so which one resolves depends on collection order.
    client = TestClient(create_app(tmp_path / "ws"))
    pid = client.post("/api/projects", json={"name": "Settings"}).json()["id"]
    return client, pid


def test_settings_route_returns_effective_defaults_for_a_fresh_project(
    tmp_path, monkeypatch
):
    from frisket.ops.egress_policy import MEDIA_PRIVATE_HOSTS_ENV

    monkeypatch.delenv(MEDIA_PRIVATE_HOSTS_ENV, raising=False)
    client, pid = _settings_project(tmp_path)

    resp = client.get(f"/api/projects/{pid}/settings")

    assert resp.status_code == 200
    assert resp.json() == {
        "media_allow_private_hosts": True,
        "media_allow_private_hosts_locked": False,
    }


def test_settings_route_patch_round_trips(tmp_path):
    client, pid = _settings_project(tmp_path)

    patched = client.patch(
        f"/api/projects/{pid}/settings", json={"media_allow_private_hosts": False}
    )

    assert patched.status_code == 200
    assert patched.json()["media_allow_private_hosts"] is False
    assert (
        client.get(f"/api/projects/{pid}/settings").json()["media_allow_private_hosts"]
        is False
    )


def test_settings_route_empty_patch_preserves_stored_values(tmp_path):
    client, pid = _settings_project(tmp_path)
    client.patch(
        f"/api/projects/{pid}/settings", json={"media_allow_private_hosts": False}
    )

    resp = client.patch(f"/api/projects/{pid}/settings", json={})

    assert resp.json()["media_allow_private_hosts"] is False


@pytest.mark.parametrize(
    "body",
    [
        {"media_allow_privat_hosts": False},
        {"media_allow_private_hosts": "yes please"},
    ],
)
def test_settings_route_rejects_bad_bodies(tmp_path, body: dict[str, Any]):
    client, pid = _settings_project(tmp_path)

    # A misspelled field must be a 422, not a silently dropped no-op that
    # reports success while changing nothing.
    assert client.patch(f"/api/projects/{pid}/settings", json=body).status_code == 422


def test_settings_route_404s_for_an_unknown_project(tmp_path):
    client, _ = _settings_project(tmp_path)

    assert client.get("/api/projects/does-not-exist/settings").status_code == 404


def test_server_deny_default_is_overridable_per_project(tmp_path, monkeypatch):
    from frisket.ops.egress_policy import MEDIA_PRIVATE_HOSTS_ENV

    monkeypatch.setenv(MEDIA_PRIVATE_HOSTS_ENV, "deny")
    client, pid = _settings_project(tmp_path)

    # deny is a default, not a lock: a fresh project inherits it...
    fresh = client.get(f"/api/projects/{pid}/settings").json()
    assert fresh == {
        "media_allow_private_hosts": False,
        "media_allow_private_hosts_locked": False,
    }

    # ...and an explicit project override still wins.
    patched = client.patch(
        f"/api/projects/{pid}/settings", json={"media_allow_private_hosts": True}
    )
    assert patched.status_code == 200
    assert patched.json()["media_allow_private_hosts"] is True


def test_server_deny_locked_hides_and_rejects_the_project_override(
    tmp_path, monkeypatch
):
    """End-to-end lock: the effective value is off, the response says locked
    (the UI hides the toggle on that flag), and PATCHing the override is a
    400 — even for a project that stored an explicit allow earlier."""
    from frisket.ops.egress_policy import MEDIA_PRIVATE_HOSTS_ENV

    monkeypatch.delenv(MEDIA_PRIVATE_HOSTS_ENV, raising=False)
    client, pid = _settings_project(tmp_path)
    assert (
        client.patch(
            f"/api/projects/{pid}/settings", json={"media_allow_private_hosts": True}
        ).status_code
        == 200
    )

    monkeypatch.setenv(MEDIA_PRIVATE_HOSTS_ENV, "deny-locked")

    locked = client.get(f"/api/projects/{pid}/settings").json()
    assert locked == {
        "media_allow_private_hosts": False,
        "media_allow_private_hosts_locked": True,
    }

    refused = client.patch(
        f"/api/projects/{pid}/settings", json={"media_allow_private_hosts": True}
    )
    assert refused.status_code == 400
    assert "locked" in refused.json()["detail"]

    # Settings the lock does not govern stay patchable.
    assert client.patch(f"/api/projects/{pid}/settings", json={}).status_code == 200
