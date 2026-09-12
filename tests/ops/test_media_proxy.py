"""The server-level media egress proxy setting: validation, resolution, and
its threading into the yt-dlp download path."""

from __future__ import annotations

import json
import socket

import pytest

from frisket.ops import egress_policy, media_proxy, ytdlp
from frisket.ops.media_proxy import (
    MEDIA_PROXY_ENV,
    MediaProxyConfig,
    read_media_proxy,
    validate_media_proxy_url,
    write_media_proxy,
)
from frisket.runtime.launch import worker_argv


@pytest.fixture
def stable_public_dns(monkeypatch) -> None:
    real_getaddrinfo = socket.getaddrinfo

    def fixture_getaddrinfo(host, port, *args, **kwargs):
        if host == "example.com":
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("8.8.8.8", port or 0),
                )
            ]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(egress_policy.socket, "getaddrinfo", fixture_getaddrinfo)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_loopback_socks5_url_validates_clean() -> None:
    validated = validate_media_proxy_url("socks5://127.0.0.1:1080")
    assert validated.url == "socks5://127.0.0.1:1080"
    assert validated.warnings == ()


@pytest.mark.parametrize("scheme", ["socks5", "socks5h", "http"])
def test_every_allowed_scheme_is_accepted(scheme: str) -> None:
    assert validate_media_proxy_url(f"{scheme}://localhost:1080").warnings == ()


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:1080",
        "socks4://127.0.0.1:1080",
        "ftp://127.0.0.1:1080",
        "127.0.0.1:1080",
        "",
        "socks5://",
        "socks5://127.0.0.1:1080/path",
        "socks5://127.0.0.1:1080?x=1",
    ],
)
def test_unusable_shapes_are_refused(url: str) -> None:
    with pytest.raises(ValueError):
        validate_media_proxy_url(url)


def test_embedded_credentials_are_refused_as_secret_material() -> None:
    with pytest.raises(ValueError, match="credentials"):
        validate_media_proxy_url("socks5://user:pass@127.0.0.1:1080")


def test_non_loopback_host_warns_but_validates() -> None:
    validated = validate_media_proxy_url("socks5://10.0.0.5:1080")
    assert validated.url == "socks5://10.0.0.5:1080"
    assert any("not loopback" in warning for warning in validated.warnings)


def test_missing_port_warns_but_validates() -> None:
    validated = validate_media_proxy_url("socks5://127.0.0.1")
    assert any("port" in warning for warning in validated.warnings)


# ---------------------------------------------------------------------------
# read/write resolution
# ---------------------------------------------------------------------------


def test_write_then_read_round_trips_via_the_state_file(tmp_path) -> None:
    write_media_proxy(tmp_path, "socks5://127.0.0.1:1080")
    resolved = read_media_proxy(tmp_path, env={})
    assert resolved == MediaProxyConfig(url="socks5://127.0.0.1:1080", source="admin")


def test_clearing_removes_the_state_file(tmp_path) -> None:
    write_media_proxy(tmp_path, "socks5://127.0.0.1:1080")
    write_media_proxy(tmp_path, None)
    assert read_media_proxy(tmp_path, env={}) == MediaProxyConfig(None, None)
    # Clearing an already-clear setting stays a no-op, not an error.
    write_media_proxy(tmp_path, None)


def test_admin_state_wins_over_the_env_var(tmp_path) -> None:
    write_media_proxy(tmp_path, "socks5://127.0.0.1:1080")
    resolved = read_media_proxy(
        tmp_path, env={MEDIA_PROXY_ENV: "socks5://127.0.0.1:9999"}
    )
    assert resolved.source == "admin"
    assert resolved.url == "socks5://127.0.0.1:1080"


def test_env_var_is_the_boot_time_fallback(tmp_path) -> None:
    resolved = read_media_proxy(
        tmp_path, env={MEDIA_PROXY_ENV: "socks5h://127.0.0.1:1080"}
    )
    assert resolved == MediaProxyConfig(url="socks5h://127.0.0.1:1080", source="env")


def test_data_dir_comes_from_the_env_when_not_passed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(media_proxy.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(MEDIA_PROXY_ENV, raising=False)
    write_media_proxy(tmp_path, "socks5://127.0.0.1:1080")
    assert media_proxy.resolve_media_proxy() == "socks5://127.0.0.1:1080"


# ---------------------------------------------------------------------------
# one data dir: the surface that REPORTS the proxy and the surface that
# EGRESSES must read the same file
# ---------------------------------------------------------------------------


def test_the_default_data_dir_matches_the_team_config_default() -> None:
    """The admin surface writes through ``config.data_dir``; the transport
    resolves its own. They are two computations of one location, so they get
    a parity test — the ops layer may not import the team layer
    (scripts/ci/import_boundaries.json, "ops-does-not-import-server-or-team"),
    so nothing else would notice either default moving.
    """
    from frisket.team.config import team_config_from_env

    for env in ({}, {"FRISKET_DATA_DIR": "/srv/frisket/data"}):
        config = team_config_from_env(
            {"FRISKET_RUN_QUEUE_DATABASE_URL": "sqlite:///q.db", **env}
        )
        transport_dir = media_proxy._state_dir(None, dict(env))
        assert config.data_dir == transport_dir.resolve(), env


def test_an_admin_set_proxy_reaches_the_download_path_without_the_env_var(
    tmp_path, monkeypatch, stable_public_dns
) -> None:
    """The bare-uvicorn deployment: no FRISKET_DATA_DIR
    anywhere, so the admin surface wrote ``./frisket-data/media_proxy.json``
    while the transport resolved NOTHING and egressed from the server's real
    IP — with ``GET /api/admin/media-proxy`` reporting configured and
    ``POST .../check`` probing successfully through the tunnel the whole time.
    """
    monkeypatch.delenv(media_proxy.DATA_DIR_ENV, raising=False)
    monkeypatch.delenv(MEDIA_PROXY_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    # Written exactly where the admin route writes it: through the team
    # config's own resolved data_dir, not through a path this test invents.
    from frisket.team.config import team_config_from_env

    admin_data_dir = team_config_from_env(
        {"FRISKET_RUN_QUEUE_DATABASE_URL": "sqlite:///q.db"}
    ).data_dir
    write_media_proxy(admin_data_dir, "socks5h://127.0.0.1:1080")

    # (a) the transport that actually egresses
    assert media_proxy.resolve_media_proxy() == "socks5h://127.0.0.1:1080"
    seen: dict = {}

    def fake_extractor(request, work_dir):
        seen.update(request)
        (work_dir / "vid1.mp4").write_bytes(b"media-bytes")
        return {
            "media_filename": "vid1.mp4",
            "sidecar_filenames": [],
            "metadata": {"yt_dlp_id": "vid1"},
            "runtime_evidence": {},
        }

    result = ytdlp.download_media(
        "https://example.com/watch?v=vid1", extractor=fake_extractor
    )
    assert seen["proxy"] == "socks5h://127.0.0.1:1080"
    assert result.metadata["proxied"] is True

    # (b) the second-order consequence: the serialization cap that keeps a
    # residential tunnel from looking like six parallel bots (the pattern
    # that provoked a live 403) also read nothing here.
    from frisket.engine.executor.media_download import download_max_concurrency

    monkeypatch.delenv("FRISKET_MEDIA_PROXY_MAX_CONCURRENCY", raising=False)
    assert download_max_concurrency() == 1


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps(["socks5://127.0.0.1:1080"]),
        json.dumps({"url": 7}),
        json.dumps({"url": "https://127.0.0.1:1080"}),
    ],
)
def test_a_corrupt_or_invalid_state_file_resolves_to_none(tmp_path, content) -> None:
    media_proxy.media_proxy_state_path(tmp_path).write_text(content)
    assert read_media_proxy(tmp_path, env={}) == MediaProxyConfig(None, None)


def _route_file(tmp_path, lines: list[str]) -> None:
    route = tmp_path / "proc" / "net" / "route"
    route.parent.mkdir(parents=True, exist_ok=True)
    header = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask"
    route.write_text("\n".join([header, *lines]) + "\n")


def test_container_gateway_reads_the_default_route(tmp_path) -> None:
    (tmp_path / ".dockerenv").touch()
    # 010012AC is 172.18.0.1 as a little-endian u32 (compose bridge gateway).
    _route_file(
        tmp_path,
        [
            "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF",
            "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000",
        ],
    )
    assert media_proxy.container_gateway(tmp_path) == "172.18.0.1"


def test_container_gateway_is_none_without_a_container_marker(tmp_path) -> None:
    # Bare metal: the default gateway is the LAN router, never a proxy bind.
    _route_file(tmp_path, ["eth0\t00000000\t0101A8C0\t0003\t0\t0\t0\t00000000"])
    assert media_proxy.container_gateway(tmp_path) is None


def test_container_gateway_tolerates_missing_or_malformed_routes(tmp_path) -> None:
    (tmp_path / ".dockerenv").touch()
    assert media_proxy.container_gateway(tmp_path) is None
    _route_file(tmp_path, ["eth0\t00000000\tnothex!!\t0003\t0\t0\t0\t00000000"])
    assert media_proxy.container_gateway(tmp_path) is None
    _route_file(tmp_path, ["eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF"])
    assert media_proxy.container_gateway(tmp_path) is None


def test_container_gateway_accepts_a_cgroup_marker(tmp_path) -> None:
    cgroup = tmp_path / "proc" / "1" / "cgroup"
    cgroup.parent.mkdir(parents=True, exist_ok=True)
    cgroup.write_text("0::/system.slice/docker-abc123.scope\n")
    _route_file(tmp_path, ["eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000"])
    assert media_proxy.container_gateway(tmp_path) == "172.18.0.1"


def test_an_invalid_env_value_resolves_to_none_and_carries_the_reason(
    tmp_path,
) -> None:
    resolved = read_media_proxy(tmp_path, env={MEDIA_PROXY_ENV: "socks4://x:1"})
    assert resolved.url is None
    assert resolved.source is None
    assert resolved.env_invalid is not None and "scheme" in resolved.env_invalid


# ---------------------------------------------------------------------------
# threading into the yt-dlp subprocess argv
# ---------------------------------------------------------------------------


class _FakePopen:
    calls: list[list[str]] = []

    def __init__(self, argv, **kwargs):
        type(self).calls.append(list(argv))
        self.returncode = 1

    def communicate(self, timeout=None):
        return "", "synthetic failure"


def _extract_argv(monkeypatch, request) -> list[str]:
    _FakePopen.calls = []
    monkeypatch.setattr(ytdlp.subprocess, "Popen", _FakePopen)
    extract = ytdlp._installed_ytdlp_cli_extractor(
        {
            "runtime_name": "yt-dlp",
            "resolved_version": "0.test",
            "artifact_sha256": "0" * 64,
        }
    )
    with pytest.raises(RuntimeError):
        extract(request, __import__("pathlib").Path("."))
    assert len(_FakePopen.calls) == 1
    return _FakePopen.calls[0]


def test_cli_extractor_passes_proxy_to_ytdlp(monkeypatch, tmp_path) -> None:
    argv = _extract_argv(
        monkeypatch,
        {
            "url": "https://example.com/watch?v=x",
            "media_type": "video",
            "proxy": "socks5://127.0.0.1:1080",
        },
    )
    assert argv[:4] == worker_argv("yt-dlp")
    assert argv[argv.index("--proxy") + 1] == "socks5://127.0.0.1:1080"
    # The explicit argument must come after config handling so it outranks any
    # admin config file value.
    assert argv.index("--proxy") > argv.index("--ignore-config")


def test_cli_extractor_omits_proxy_when_unset(monkeypatch) -> None:
    argv = _extract_argv(
        monkeypatch,
        {"url": "https://example.com/watch?v=x", "media_type": "video"},
    )
    assert "--proxy" not in argv


def test_download_media_threads_the_setting_and_records_the_boolean(
    monkeypatch, tmp_path, stable_public_dns
) -> None:
    monkeypatch.setenv(media_proxy.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(MEDIA_PROXY_ENV, raising=False)
    write_media_proxy(tmp_path, "socks5://127.0.0.1:1080")

    seen: dict = {}

    def fake_extractor(request, work_dir):
        seen.update(request)
        (work_dir / "vid1.mp4").write_bytes(b"media-bytes")
        return {
            "media_filename": "vid1.mp4",
            "sidecar_filenames": [],
            "metadata": {"yt_dlp_id": "vid1"},
            "runtime_evidence": {},
        }

    result = ytdlp.download_media(
        "https://example.com/watch?v=vid1", extractor=fake_extractor
    )
    assert seen["proxy"] == "socks5://127.0.0.1:1080"
    assert result.metadata["proxied"] is True
    # The URL itself must never ride into receipt-bound metadata.
    assert "socks5" not in json.dumps(result.metadata)


def test_download_media_metadata_omits_proxied_when_no_proxy(
    monkeypatch, tmp_path, stable_public_dns
) -> None:
    monkeypatch.delenv(MEDIA_PROXY_ENV, raising=False)
    monkeypatch.delenv(media_proxy.DATA_DIR_ENV, raising=False)
    # With no FRISKET_DATA_DIR the state file is looked for under the default
    # data dir RELATIVE to cwd, so this case is only "no proxy" if cwd is
    # empty — pin it rather than depending on where pytest was invoked.
    monkeypatch.chdir(tmp_path)

    def fake_extractor(request, work_dir):
        assert request["proxy"] is None
        (work_dir / "vid1.mp4").write_bytes(b"media-bytes")
        return {
            "media_filename": "vid1.mp4",
            "sidecar_filenames": [],
            "metadata": {"yt_dlp_id": "vid1"},
            "runtime_evidence": {},
        }

    result = ytdlp.download_media(
        "https://example.com/watch?v=vid1", extractor=fake_extractor
    )
    assert "proxied" not in result.metadata
