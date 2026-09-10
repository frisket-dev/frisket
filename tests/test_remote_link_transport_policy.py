"""Transport-policy pins for ``frisket remote link``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from frisket.remote import cli, config_store


@pytest.mark.parametrize(
    "url",
    (
        "https://frisket.example.test",
        "http://localhost:8000",
        "http://api.localhost:8000",
        "http://127.0.0.2:8000",
        "http://[::1]:8000",
    ),
)
def test_server_url_transport_policy_allows_https_and_loopback_http(url: str) -> None:
    assert config_store.valid_server_url(url) is True


def test_server_url_transport_policy_requires_escape_for_remote_http() -> None:
    url = "http://frisket.example.test"
    assert config_store.valid_server_url(url) is False
    assert config_store.valid_server_url(url, allow_insecure_http=True) is True


def test_remote_link_refuses_remote_http_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class UnexpectedClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("transport refusal must happen before network I/O")

    monkeypatch.setattr(cli, "RemoteClient", UnexpectedClient)

    assert (
        cli.remote_command(
            [
                "link",
                "http://frisket.example.test",
                "--token",
                "operator-token",
            ]
        )
        == cli.EXIT_VALIDATION
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--insecure-http" in captured.err
    assert captured.err.strip().count("\n") == 0


def test_remote_link_insecure_http_escape_is_explicit_and_persists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    class FakeClient:
        def __init__(self, url: str, token: str) -> None:
            seen.update(url=url, token=token)

        def ping(self) -> dict[str, str]:
            return {
                "org": "Example",
                "server_version": "test",
                "token_label": "laptop",
            }

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(cli, "RemoteClient", FakeClient)
    url = "http://frisket.example.test"

    assert (
        cli.remote_command(
            [
                "link",
                url,
                "--token",
                "operator-token",
                "--name",
                "insecure-test",
                "--insecure-http",
            ]
        )
        == cli.EXIT_OK
    )
    assert seen == {"url": url, "token": "operator-token"}
    stored = config_store.load_config()
    assert stored["servers"]["insecure-test"]["url"] == url
