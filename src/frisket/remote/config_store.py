"""Named server profiles for the remote CLI, ssh-config style, in TOML.

`~/.config/frisket/config.toml` (0600) holds one `[servers.<name>]` table per
linked deployment plus a top-level `default_server`. Every remote verb
resolves its target as: explicit `--server` (name or URL) > `FRISKET_SERVER`
env (name or URL) > the default profile.
"""

from __future__ import annotations

import ipaddress
import json
import os
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class ConfigError(ValueError):
    """A profile store problem the operator can fix (exit code 3)."""


def config_path(env: dict[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    base = values.get("XDG_CONFIG_HOME") or ""
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "frisket" / "config.toml"


def load_config(path: Path | None = None) -> dict[str, Any]:
    target = path or config_path()
    if not target.is_file():
        return {"default_server": None, "servers": {}}
    try:
        parsed = tomllib.loads(target.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {target}: {exc}") from exc
    servers = parsed.get("servers")
    if servers is not None and not isinstance(servers, dict):
        raise ConfigError(f"{target}: [servers] must be a table")
    return {
        "default_server": parsed.get("default_server"),
        "servers": dict(servers or {}),
    }


def _toml_string(value: str) -> str:
    # TOML basic strings accept exactly the escape repertoire json.dumps
    # emits (\" \\ \n \r \t \uXXXX), so this stays a valid TOML scalar.
    return json.dumps(value)


def _render(config: dict[str, Any]) -> str:
    lines: list[str] = []
    default_server = config.get("default_server")
    if default_server:
        lines.append(f"default_server = {_toml_string(str(default_server))}")
        lines.append("")
    for name in sorted(config.get("servers", {})):
        entry = config["servers"][name]
        lines.append(f"[servers.{_toml_string(str(name))}]")
        for key in ("url", "token", "label", "linked_at"):
            value = entry.get(key)
            if value is not None:
                lines.append(f"{key} = {_toml_string(str(value))}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def save_config(config: dict[str, Any], path: Path | None = None) -> Path:
    """Atomic 0600 write (temp file + rename) under a 0700 config dir."""
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.parent.chmod(0o700)
    except OSError:
        pass
    fd, temp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".config-", suffix=".toml"
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(_render(config))
        os.replace(temp_name, target)
    except OSError:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return target


def normalized_url(url: str) -> str:
    return url.strip().rstrip("/")


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def valid_server_url(url: str, *, allow_insecure_http: bool = False) -> bool:
    try:
        parsed = urlparse(url.strip())
        hostname = parsed.hostname
    except ValueError:
        return False
    if not parsed.netloc or parsed.scheme not in {"http", "https"}:
        return False
    return (
        parsed.scheme == "https" or _is_loopback_host(hostname) or allow_insecure_http
    )


def default_profile_name(url: str) -> str:
    return urlparse(normalized_url(url)).hostname or "server"


def upsert_server(
    config: dict[str, Any],
    *,
    name: str,
    url: str,
    token: str,
    label: str | None,
) -> dict[str, Any]:
    servers = dict(config.get("servers", {}))
    entry = {
        "url": normalized_url(url),
        "token": token,
        "linked_at": datetime.now(UTC).isoformat(),
    }
    if label:
        entry["label"] = label
    servers[name] = entry
    default_server = config.get("default_server")
    if not default_server or default_server not in servers:
        default_server = name
    return {"default_server": default_server, "servers": servers}


def resolve_server(
    config: dict[str, Any],
    *,
    selector: str | None,
    env: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Resolve a verb's target profile: --server > FRISKET_SERVER > default."""
    values = os.environ if env is None else env
    servers: dict[str, Any] = config.get("servers", {})
    wanted = selector or values.get("FRISKET_SERVER") or ""
    wanted = wanted.strip()
    if wanted:
        if "://" in wanted:
            clean = normalized_url(wanted)
            for name, entry in servers.items():
                if normalized_url(str(entry.get("url", ""))) == clean:
                    return name, entry
            raise ConfigError(
                f"no linked server matches {clean}; run: frisket remote link {clean} --token <token>"
            )
        if wanted in servers:
            return wanted, servers[wanted]
        raise ConfigError(
            f"no server profile named '{wanted}'; see: frisket remote list"
        )
    default_server = config.get("default_server")
    if default_server and default_server in servers:
        return str(default_server), servers[str(default_server)]
    if not servers:
        raise ConfigError(
            "no servers linked yet; run: frisket remote link <url> --token <token>"
        )
    raise ConfigError(
        "no default server set; pass --server or run: frisket remote default <name>"
    )


__all__ = [
    "ConfigError",
    "config_path",
    "default_profile_name",
    "load_config",
    "normalized_url",
    "resolve_server",
    "save_config",
    "upsert_server",
    "valid_server_url",
]
