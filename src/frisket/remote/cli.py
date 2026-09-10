"""`frisket remote` / `frisket users` / `frisket token` / `frisket secrets` /
`frisket proxy`.

Exit-code contract: 0 success, 2 auth/pairing failure, 3 validation error,
1 anything else. Every failure is one human-readable line on stderr; no
tracebacks, and no secret value is ever echoed.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from frisket.remote import config_store
from frisket.remote.client import (
    RemoteClient,
    RemoteError,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AUTH = 2
EXIT_VALIDATION = 3


class UsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    """Argparse that reports usage problems on the validation exit code."""

    def error(self, message: str) -> Any:  # noqa: D102 - argparse contract
        raise UsageError(f"{self.prog}: {message}")


def _say(message: str) -> None:
    print(message)


def _warn(message: str) -> None:
    print(message, file=sys.stderr)


def _dispatch(handler: Any, argv: list[str]) -> int:
    try:
        return int(handler(argv))
    except UsageError as exc:
        _warn(f"frisket: {exc}")
        return EXIT_VALIDATION
    except config_store.ConfigError as exc:
        _warn(f"frisket: {exc}")
        return EXIT_VALIDATION
    except RemoteError as exc:
        _warn(f"frisket: {exc}")
        return exc.exit_code
    except KeyboardInterrupt:
        _warn("frisket: cancelled")
        return EXIT_ERROR


def _server_argument(parser: _Parser) -> None:
    parser.add_argument(
        "--server",
        default=None,
        help="server profile name or URL (default: FRISKET_SERVER, then the default profile)",
    )


def _client_for(selector: str | None) -> tuple[str, dict[str, Any], RemoteClient]:
    config = config_store.load_config()
    name, entry = config_store.resolve_server(config, selector=selector)
    return name, entry, RemoteClient(str(entry["url"]), str(entry["token"]))


def _iso_age(linked_at: str | None) -> str:
    if not linked_at:
        return "unknown age"
    try:
        stamp = datetime.fromisoformat(linked_at)
    except ValueError:
        return "unknown age"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    seconds = max(0, int((datetime.now(UTC) - stamp).total_seconds()))
    if seconds < 3600:
        return f"{seconds // 60}m old"
    if seconds < 86400:
        return f"{seconds // 3600}h old"
    return f"{seconds // 86400}d old"


def _table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
        for row in rows
    )


# ---------------------------------------------------------------------------
# frisket remote
# ---------------------------------------------------------------------------


def _remote_link(argv: list[str]) -> int:
    parser = _Parser(prog="frisket remote link")
    parser.add_argument("url")
    parser.add_argument(
        "--token", required=True, help="operator token minted on the server"
    )
    parser.add_argument(
        "--name", default=None, help="profile name (default: the server hostname)"
    )
    parser.add_argument(
        "--insecure-http",
        action="store_true",
        help="allow plain HTTP to a non-loopback server",
    )
    args = parser.parse_args(argv)
    url = config_store.normalized_url(args.url)
    if not config_store.valid_server_url(url, allow_insecure_http=args.insecure_http):
        if config_store.valid_server_url(url, allow_insecure_http=True):
            raise UsageError(
                "plain HTTP is only allowed for loopback servers; "
                "pass --insecure-http to override"
            )
        raise UsageError(f"'{args.url}' is not an http(s) server URL")
    client = RemoteClient(url, args.token)
    ping = client.ping()
    name = args.name or config_store.default_profile_name(url)
    config = config_store.load_config()
    updated = config_store.upsert_server(
        config,
        name=name,
        url=url,
        token=args.token,
        label=ping.get("token_label"),
    )
    config_store.save_config(updated)
    default_note = " (default)" if updated["default_server"] == name else ""
    _say(
        f"Linked '{name}'{default_note}: {url} — org '{ping.get('org')}', "
        f"server version {ping.get('server_version')}"
    )
    return EXIT_OK


def _remote_status(argv: list[str]) -> int:
    parser = _Parser(prog="frisket remote status")
    _server_argument(parser)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    name, entry, client = _client_for(args.server)
    ready = client.ready()
    ping = client.ping()
    if args.as_json:
        _say(
            json.dumps(
                {"server": name, "url": entry["url"], "ready": ready, "ping": ping},
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_OK
    _say(f"{name}: {entry['url']}")
    if ready.get("ok"):
        _say("  ready: ok")
    else:
        failures = (
            ", ".join(str(item) for item in ready.get("failures", [])) or "unknown"
        )
        _say(f"  ready: NOT ready ({failures})")
    label = ping.get("token_label") or "unlabeled"
    _say(
        f"  org: {ping.get('org')}   server version: {ping.get('server_version')}   "
        f"token: {label}"
    )
    return EXIT_OK


def _remote_list(argv: list[str]) -> int:
    parser = _Parser(prog="frisket remote list")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    config = config_store.load_config()
    servers = config.get("servers", {})
    default_server = config.get("default_server")
    if args.as_json:
        payload = {
            "default_server": default_server,
            "servers": {
                name: {
                    "url": entry.get("url"),
                    "label": entry.get("label"),
                    "linked_at": entry.get("linked_at"),
                }
                for name, entry in servers.items()
            },
        }
        _say(json.dumps(payload, indent=2, sort_keys=True))
        return EXIT_OK
    if not servers:
        _say("No servers linked. Run: frisket remote link <url> --token <token>")
        return EXIT_OK
    rows = [["NAME", "URL", "DEFAULT", "TOKEN"]]
    for name in sorted(servers):
        entry = servers[name]
        token_note = (
            f"{entry.get('label') or 'unlabeled'} ({_iso_age(entry.get('linked_at'))})"
        )
        rows.append(
            [
                name,
                str(entry.get("url", "")),
                "*" if name == default_server else "",
                token_note,
            ]
        )
    _say(_table(rows))
    return EXIT_OK


def _remote_default(argv: list[str]) -> int:
    parser = _Parser(prog="frisket remote default")
    parser.add_argument("name")
    args = parser.parse_args(argv)
    config = config_store.load_config()
    if args.name not in config.get("servers", {}):
        raise config_store.ConfigError(
            f"no server profile named '{args.name}'; see: frisket remote list"
        )
    config["default_server"] = args.name
    config_store.save_config(config)
    _say(f"Default server is now '{args.name}'.")
    return EXIT_OK


_REMOTE_VERBS = {
    "link": _remote_link,
    "status": _remote_status,
    "list": _remote_list,
    "default": _remote_default,
}


def remote_command(argv: list[str]) -> int:
    def run(rest: list[str]) -> int:
        if not rest or rest[0] in {"-h", "--help"}:
            _say(
                "Usage: frisket remote link <url> --token <token> [--name NAME] "
                "[--insecure-http]\n"
                "       frisket remote status [--server NAME|URL] [--json]\n"
                "       frisket remote list [--json]\n"
                "       frisket remote default <name>"
            )
            return EXIT_OK if rest else EXIT_VALIDATION
        verb = _REMOTE_VERBS.get(rest[0])
        if verb is None:
            raise UsageError(f"unknown remote subcommand '{rest[0]}'")
        return verb(rest[1:])

    return _dispatch(run, argv)


# ---------------------------------------------------------------------------
# frisket token
# ---------------------------------------------------------------------------


def token_command(argv: list[str]) -> int:
    def run(rest: list[str]) -> int:
        if not rest or rest[0] in {"-h", "--help"}:
            _say("Usage: frisket token rotate [--server NAME|URL]")
            return EXIT_OK if rest else EXIT_VALIDATION
        if rest[0] != "rotate":
            raise UsageError(f"unknown token subcommand '{rest[0]}'")
        parser = _Parser(prog="frisket token rotate")
        _server_argument(parser)
        args = parser.parse_args(rest[1:])
        config = config_store.load_config()
        name, entry = config_store.resolve_server(config, selector=args.server)
        client = RemoteClient(str(entry["url"]), str(entry["token"]))
        rotated = client.rotate_token()
        new_token = str(rotated["token"])
        updated = config_store.upsert_server(
            config,
            name=name,
            url=str(entry["url"]),
            token=new_token,
            label=rotated.get("label") or entry.get("label"),
        )
        updated["default_server"] = (
            config.get("default_server") or updated["default_server"]
        )
        try:
            config_store.save_config(updated)
        except OSError as exc:
            # The old token is already invalidated server-side; losing the
            # replacement would strand the pairing, so surface it once.
            _warn(
                f"frisket: rotated on the server but could not update the local store ({exc})"
            )
            _say(f"New operator token (store it manually): {new_token}")
            return EXIT_ERROR
        _say(f"Rotated operator token for '{name}'; local credential store updated.")
        return EXIT_OK

    return _dispatch(run, argv)


# ---------------------------------------------------------------------------
# frisket users
# ---------------------------------------------------------------------------


def _users_add(argv: list[str]) -> int:
    parser = _Parser(prog="frisket users add")
    parser.add_argument("emails", nargs="+", metavar="email")
    parser.add_argument(
        "--role",
        default=None,
        help="role for a NEW invite (member|owner); a pending invite keeps "
        "its stored role and a conflicting --role is refused",
    )
    _server_argument(parser)
    args = parser.parse_args(argv)
    _, _, client = _client_for(args.server)
    worst = EXIT_OK
    for email in args.emails:
        try:
            added = client.users_add(email, role=args.role)
        except RemoteError as exc:
            _warn(f"frisket: {email}: {exc}")
            worst = worst or exc.exit_code
            continue
        reissued = " [re-issued]" if added.get("reissued") else ""
        _say(f"{added['email']} ({added['role']}){reissued}: {added['invite_link']}")
    return worst


def _users_list(argv: list[str]) -> int:
    parser = _Parser(prog="frisket users list")
    parser.add_argument("--json", action="store_true", dest="as_json")
    _server_argument(parser)
    args = parser.parse_args(argv)
    _, _, client = _client_for(args.server)
    listed = client.users_list()
    if args.as_json:
        _say(json.dumps(listed, indent=2, sort_keys=True))
        return EXIT_OK
    rows_payload = [
        {
            **user,
            "status": "active",
        }
        for org in listed.get("orgs", [])
        for user in org.get("users", [])
    ] + [
        {
            **invite,
            "status": "invited",
            "created_at": None,
        }
        for org in listed.get("orgs", [])
        for invite in org.get("pending_invites", [])
    ]
    if not rows_payload:
        _say("No users or pending invites.")
        return EXIT_OK
    rows = [["EMAIL", "ROLE", "STATUS", "CREATED"]]
    for user in rows_payload:
        rows.append(
            [
                str(user.get("email", "")),
                str(user.get("role", "")),
                str(user.get("status", "")),
                str(user.get("created_at") or "-"),
            ]
        )
    _say(_table(rows))
    return EXIT_OK


def _users_remove(argv: list[str]) -> int:
    parser = _Parser(prog="frisket users remove")
    parser.add_argument("email")
    _server_argument(parser)
    args = parser.parse_args(argv)
    _, _, client = _client_for(args.server)
    client.users_remove(args.email)
    _say(f"Removed {args.email} (pending invite revoked or membership deactivated).")
    return EXIT_OK


def _users_reset(argv: list[str]) -> int:
    parser = _Parser(prog="frisket users reset")
    parser.add_argument("email")
    _server_argument(parser)
    args = parser.parse_args(argv)
    _, _, client = _client_for(args.server)
    reset = client.users_reset(args.email)
    _say(f"{reset['email']}: {reset['reset_link']}")
    _say("The link is single-use and expires in about an hour.")
    return EXIT_OK


def _users_role(argv: list[str]) -> int:
    parser = _Parser(prog="frisket users role")
    parser.add_argument("email")
    parser.add_argument("role")
    _server_argument(parser)
    args = parser.parse_args(argv)
    _, _, client = _client_for(args.server)
    changed = client.users_role(args.email, args.role)
    _say(f"{changed['email']} is now '{changed['role']}' ({changed['status']}).")
    return EXIT_OK


_USERS_VERBS = {
    "add": _users_add,
    "list": _users_list,
    "remove": _users_remove,
    "reset": _users_reset,
    "role": _users_role,
}


def users_command(argv: list[str]) -> int:
    def run(rest: list[str]) -> int:
        if not rest or rest[0] in {"-h", "--help"}:
            _say(
                "Usage: frisket users add <email>... [--role member|owner] [--server ...]\n"
                "       frisket users list [--json] [--server ...]\n"
                "       frisket users remove <email> [--server ...]\n"
                "       frisket users reset <email> [--server ...]\n"
                "       frisket users role <email> <member|owner> [--server ...]"
            )
            return EXIT_OK if rest else EXIT_VALIDATION
        verb = _USERS_VERBS.get(rest[0])
        if verb is None:
            raise UsageError(f"unknown users subcommand '{rest[0]}'")
        return verb(rest[1:])

    return _dispatch(run, argv)


# ---------------------------------------------------------------------------
# frisket secrets
# ---------------------------------------------------------------------------


def _known_providers(client: RemoteClient) -> dict[str, dict[str, Any]]:
    return {
        str(entry["provider"]): entry
        for entry in client.secrets_list().get("providers", [])
    }


def _secret_value(provider: str, *, use_stdin: bool) -> str:
    if use_stdin:
        value = sys.stdin.read()
    elif sys.stdin.isatty():
        import getpass

        value = getpass.getpass(f"Value for {provider}: ")
    else:
        raise UsageError("no TTY to prompt on; pipe the value and pass --stdin")
    value = value.rstrip("\r\n")
    if not value:
        raise UsageError("the provider key must not be empty")
    return value


def _require_known_provider(providers: dict[str, dict[str, Any]], provider: str) -> str:
    clean = provider.lower().strip()
    if clean not in providers:
        known = ", ".join(sorted(providers))
        raise UsageError(f"unknown provider '{provider}' (known: {known})")
    return clean


def _secrets_list(argv: list[str]) -> int:
    parser = _Parser(prog="frisket secrets list")
    parser.add_argument("--json", action="store_true", dest="as_json")
    _server_argument(parser)
    args = parser.parse_args(argv)
    _, _, client = _client_for(args.server)
    listed = client.secrets_list()
    if args.as_json:
        _say(json.dumps(listed, indent=2, sort_keys=True))
        return EXIT_OK
    rows = [["PROVIDER", "STORED", "SOURCE"]]
    warnings: list[str] = []
    for entry in listed.get("providers", []):
        provider = str(entry.get("provider"))
        stored = str(entry.get("hint")) if entry.get("configured") else "-"
        source = str(entry.get("source") or "not configured")
        rows.append([provider, stored, source])
        if entry.get("env_present"):
            warnings.append(
                f"note: the server process also sets {entry.get('env_var')}; "
                f"that environment value can shadow the stored {provider} key"
            )
    _say(_table(rows))
    for warning in warnings:
        _warn(warning)
    return EXIT_OK


def _secrets_set(argv: list[str]) -> int:
    parser = _Parser(prog="frisket secrets set")
    parser.add_argument("provider")
    parser.add_argument(
        "--stdin",
        action="store_true",
        dest="use_stdin",
        help="read the value from stdin instead of prompting",
    )
    _server_argument(parser)
    args = parser.parse_args(argv)
    _, _, client = _client_for(args.server)
    provider = _require_known_provider(_known_providers(client), args.provider)
    value = _secret_value(provider, use_stdin=args.use_stdin)
    stored = client.secrets_set(provider, value)
    _say(f"Stored {provider} key ({stored.get('hint')}).")
    return EXIT_OK


def _secrets_unset(argv: list[str]) -> int:
    parser = _Parser(prog="frisket secrets unset")
    parser.add_argument("provider")
    _server_argument(parser)
    args = parser.parse_args(argv)
    _, _, client = _client_for(args.server)
    provider = _require_known_provider(_known_providers(client), args.provider)
    removed = client.secrets_unset(provider)
    if removed.get("deleted"):
        _say(f"Removed the stored {provider} key.")
    else:
        _say(f"No stored {provider} key to remove.")
    return EXIT_OK


def _push_source_values(
    providers: dict[str, dict[str, Any]],
    *,
    env_file: Path | None,
    from_environment: bool,
) -> dict[str, str]:
    """Allowlist-only extraction: exactly the provider env vars the server
    maps, everything else silently ignored, no value ever echoed."""
    from frisket.operability.control.env_values import EnvValueError, value_for_name

    values: dict[str, str] = {}
    for provider, entry in providers.items():
        env_name = entry.get("env_var")
        if not env_name:
            continue
        try:
            value = value_for_name(
                env_file if env_file is not None else Path(".env"),
                str(env_name),
                from_environment=from_environment,
            )
        except EnvValueError as exc:
            raise UsageError(str(exc)) from exc
        if value:
            values[provider] = value
    return values


def _secrets_push(argv: list[str]) -> int:
    parser = _Parser(prog="frisket secrets push")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--file", default=None, help=".env file (default ./.env)")
    source.add_argument(
        "--from-env",
        action="store_true",
        dest="from_environment",
        help="read from this process environment instead of a file",
    )
    parser.add_argument("--dry-run", action="store_true", dest="dry_run")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="also unset server keys whose env var is absent from the source",
    )
    parser.add_argument(
        "--yes", action="store_true", help="skip the prune confirmation"
    )
    _server_argument(parser)
    args = parser.parse_args(argv)

    env_file: Path | None = None
    if not args.from_environment:
        env_file = Path(args.file) if args.file else Path(".env")
        if not env_file.is_file():
            raise UsageError(f"env file not found: {env_file}")

    from frisket.team.security.secrets import key_hint

    _, _, client = _client_for(args.server)
    providers = _known_providers(client)
    source_values = _push_source_values(
        providers, env_file=env_file, from_environment=args.from_environment
    )

    plan: list[tuple[str, str]] = []
    for provider in sorted(providers):
        entry = providers[provider]
        if not entry.get("env_var"):
            continue
        value = source_values.get(provider)
        if value is not None:
            if entry.get("configured") and entry.get("hint") == key_hint(value):
                plan.append((provider, "in-sync"))
            elif entry.get("configured"):
                plan.append((provider, "would-update"))
            else:
                plan.append((provider, "would-add"))
        elif args.prune and entry.get("configured"):
            plan.append((provider, "would-remove"))

    if not plan:
        _say("Nothing to push: the source has no known provider keys.")
        return EXIT_OK
    for provider, action in plan:
        _say(f"{provider}: {action}")
    if args.dry_run:
        return EXIT_OK

    removals = [provider for provider, action in plan if action == "would-remove"]
    if removals and not args.yes:
        if not sys.stdin.isatty():
            raise UsageError(
                "--prune would remove "
                + ", ".join(removals)
                + "; rerun with --yes to confirm"
            )
        reply = input(f"Remove stored keys for {', '.join(removals)}? [y/N] ")
        if reply.strip().lower() not in {"y", "yes"}:
            _say("Prune skipped; nothing removed.")
            removals = []

    pushed = 0
    for provider, action in plan:
        if action in {"would-update", "would-add"}:
            client.secrets_set(provider, source_values[provider])
            pushed += 1
    for provider in removals:
        client.secrets_unset(provider)

    after = _known_providers(client)
    for provider, action in plan:
        if action in {"would-update", "would-add"} and after.get(provider, {}).get(
            "env_present"
        ):
            _warn(
                f"WARNING: the server process sets {after[provider].get('env_var')} — "
                f"that environment value can shadow the {provider} key you just pushed"
            )
    _say(
        f"Pushed {pushed} key(s); removed {len(removals)}; "
        f"{sum(1 for _p, a in plan if a == 'in-sync')} already in sync."
    )
    return EXIT_OK


_SECRETS_VERBS = {
    "list": _secrets_list,
    "set": _secrets_set,
    "unset": _secrets_unset,
    "push": _secrets_push,
}


def secrets_command(argv: list[str]) -> int:
    def run(rest: list[str]) -> int:
        if not rest or rest[0] in {"-h", "--help"}:
            _say(
                "Usage: frisket secrets set <provider> [--stdin] [--server ...]\n"
                "       frisket secrets list [--json] [--server ...]\n"
                "       frisket secrets unset <provider> [--server ...]\n"
                "       frisket secrets push [--file PATH | --from-env] [--dry-run] "
                "[--prune] [--yes] [--server ...]"
            )
            return EXIT_OK if rest else EXIT_VALIDATION
        verb = _SECRETS_VERBS.get(rest[0])
        if verb is None:
            raise UsageError(f"unknown secrets subcommand '{rest[0]}'")
        return verb(rest[1:])

    return _dispatch(run, argv)


# ---------------------------------------------------------------------------
# frisket proxy
# ---------------------------------------------------------------------------

# Reverse dynamic forwarding (-R with a port and no destination) needs the
# OPERATOR's ssh client to be OpenSSH 7.6+; the server side is ordinary
# remote forwarding and needs nothing special.
_PROXY_MIN_OPENSSH = (7, 6)
_PROXY_DEFAULT_PORT = 1080
_PROXY_CHECK_ATTEMPTS = 10
_PROXY_CHECK_DELAY_SECONDS = 2.0

_PROXY_FALLBACK_NOTE = (
    "Fallback for older OpenSSH: run a small SOCKS server on this computer "
    "(for example `microsocks -p 1080`) and forward it with\n"
    "  ssh -N -R 127.0.0.1:1080:127.0.0.1:1080 <target>\n"
    "A plain local `ssh -D 1080` is NOT a substitute: it egresses from "
    "wherever that ssh client connects to, not from this computer."
)


def _openssh_version() -> tuple[int, int] | None:
    """The local ssh client's OpenSSH version, or None when undetectable."""
    try:
        proc = subprocess.run(  # noqa: S603
            ["ssh", "-V"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"OpenSSH[_a-zA-Z]*[_-](\d+)\.(\d+)", proc.stderr + proc.stdout)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))


def _proxy_ssh_target(entry: dict[str, Any], override: str | None) -> tuple[str, bool]:
    """The ssh destination and whether it was derived from the profile URL."""
    if override:
        return override, False
    host = urlparse(str(entry.get("url") or "")).hostname
    if not host:
        raise UsageError(
            "cannot derive an ssh target from the server profile URL; "
            "pass --ssh user@host"
        )
    return f"root@{host}", True


def _proxy_ssh_argv(target: str, bind_host: str, port: int) -> list[str]:
    # ExitOnForwardFailure: an sshd that refuses the remote listen
    # (AllowTcpForwarding/PermitListen, or a stale tunnel already holding the
    # port) must exit immediately instead of idling as a tunnel that can
    # never carry traffic. The keepalives bound how long a NAT-dropped
    # connection can linger before ssh notices it is dead — the babysitter's
    # clear-on-death only fires once ssh actually exits.
    return [
        "ssh",
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-R",
        f"{bind_host}:{port}",
        target,
    ]


def _proxy_gatewayports_note(target: str, bind_host: str) -> None:
    """The one non-loopback-bind failure sshd cannot report: with the
    default ``GatewayPorts no`` it silently rebinds a non-loopback remote
    forward to loopback — the forward "succeeds", ExitOnForwardFailure never
    fires, and the server can never reach it. Only the operator can fix
    that, so print the exact one-time server-side recipe."""
    user = target.split("@", 1)[0] if "@" in target else "root"
    _warn(
        "frisket: if the tunnel is running but stays unreachable, the "
        "server's sshd has likely rebound the forward to loopback (its "
        "default GatewayPorts no), so nothing is listening at "
        f"{bind_host}. One-time fix, run on the server:\n"
        f"  printf 'Match User {user}\\n    GatewayPorts clientspecified\\n'"
        " > /etc/ssh/sshd_config.d/90-frisket-proxy.conf\n"
        "  sshd -t && systemctl reload ssh\n"
        "then rerun this command."
    )


def _proxy_openssh_note(*, required: bool) -> bool:
    """Print version guidance; True when the local client is usable."""
    version = _openssh_version()
    if version is None:
        _warn(
            "frisket: could not detect the local OpenSSH version; reverse "
            "dynamic forwarding needs OpenSSH 7.6+"
        )
        return True
    if version >= _PROXY_MIN_OPENSSH:
        return True
    message = (
        f"local OpenSSH {version[0]}.{version[1]} does not support reverse "
        "dynamic forwarding (-R <port> with no destination needs OpenSSH 7.6+)."
    )
    if required:
        _warn(f"frisket: {message}")
        _warn(_PROXY_FALLBACK_NOTE)
    else:
        _warn(f"frisket: {message} The printed command will fail on this machine.")
        _warn(_PROXY_FALLBACK_NOTE)
    return False


def _proxy_report_check(check: dict[str, Any]) -> bool:
    if check.get("ok"):
        status = check.get("status_code")
        elapsed = check.get("elapsed_ms")
        _say(f"Proxy connected: probe returned HTTP {status} in {elapsed} ms.")
        return True
    error = check.get("error") or "probe failed"
    _say(f"Proxy not reachable yet: {error}")
    return False


def _proxy_wait_until_connected(
    client: RemoteClient, process: subprocess.Popen[bytes]
) -> bool:
    for attempt in range(_PROXY_CHECK_ATTEMPTS):
        if process.poll() is not None:
            return False
        try:
            check: dict[str, Any] = client.media_proxy_check()
        except RemoteError as exc:
            # A transient server error mid-poll is "not connected yet", not a
            # reason to abort the babysit and orphan the ssh child.
            check = {"ok": False, "error": str(exc)}
        if check.get("ok"):
            _proxy_report_check(check)
            return True
        if attempt < _PROXY_CHECK_ATTEMPTS - 1:
            time.sleep(_PROXY_CHECK_DELAY_SECONDS)
        else:
            _proxy_report_check(check)
    return False


def _proxy_up(argv: list[str]) -> int:
    parser = _Parser(prog="frisket proxy up")
    _server_argument(parser)
    parser.add_argument(
        "--ssh",
        default=None,
        metavar="USER@HOST",
        help="ssh destination for the tunnel (default: root@<server profile host>)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_PROXY_DEFAULT_PORT,
        help=f"server-side SOCKS port (default: {_PROXY_DEFAULT_PORT})",
    )
    parser.add_argument(
        "--bind",
        default=None,
        metavar="ADDR",
        help="server-side address the tunnel listens on (default: the "
        "server's container gateway when it reports one, else 127.0.0.1)",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="start the ssh tunnel from here and keep it running (Ctrl-C stops "
        "it and clears the server setting)",
    )
    args = parser.parse_args(argv)
    if not (0 < args.port < 65536):
        raise UsageError("--port must be between 1 and 65535")
    name, entry, client = _client_for(args.server)
    # A containerized server (the standard Docker install) cannot reach a
    # host-loopback listener — its 127.0.0.1 is the container's own. The
    # server reports its bridge gateway; the tunnel binds there instead.
    # --bind overrides both, for servers that cannot report one.
    gateway = str(client.media_proxy_get().get("container_gateway") or "") or None
    bind_host = args.bind or gateway or "127.0.0.1"

    target, derived = _proxy_ssh_target(entry, args.ssh)
    if derived:
        _say(
            f"Tunnel target: {target} (derived from server profile '{name}'; "
            "override with --ssh user@host)"
        )
    else:
        _say(f"Tunnel target: {target}")
    if gateway and bind_host == gateway:
        _say(
            f"Server is containerized: the tunnel binds {gateway} (its bridge "
            "gateway) because the server cannot reach the host's loopback."
        )
    ssh_argv = _proxy_ssh_argv(target, bind_host, args.port)
    ssh_ok = _proxy_openssh_note(required=args.run)
    if args.run and not ssh_ok:
        return EXIT_ERROR

    # socks5h: hostnames resolve at the tunnel's far end (the operator's
    # machine), so DNS answers stay geo-consistent with the egress IP.
    proxy_url = f"socks5h://{bind_host}:{args.port}"
    stored = client.media_proxy_set(proxy_url)
    for warning in stored.get("warnings") or []:
        if bind_host != "127.0.0.1" and "not loopback" in warning:
            # The non-loopback shape was chosen deliberately (container
            # gateway or --bind); the generic warning would only confuse.
            continue
        _warn(f"frisket: {warning}")
    _say(f"Media proxy on '{name}' set to {proxy_url}.")

    if not args.run:
        _say("Run this on your own computer and leave it running:")
        _say(f"  {shlex.join(ssh_argv)}")
        connected = _proxy_report_check(client.media_proxy_check())
        if not connected:
            _say(
                "Start the tunnel, then `frisket proxy status` to confirm the "
                "server can reach it."
            )
            if bind_host != "127.0.0.1":
                _proxy_gatewayports_note(target, bind_host)
        return EXIT_OK

    _say(f"Starting: {shlex.join(ssh_argv)}")
    try:
        process = subprocess.Popen(ssh_argv)  # noqa: S603
    except OSError as exc:
        client.media_proxy_clear()
        _warn(f"frisket: could not start ssh ({exc}); media proxy cleared")
        return EXIT_ERROR
    try:
        connected = _proxy_wait_until_connected(client, process)
        if process.poll() is not None:
            _warn(
                f"frisket: ssh exited with code {process.returncode} before the "
                "tunnel came up"
            )
            return EXIT_ERROR
        if connected:
            _say("Tunnel is up. Leave this running; Ctrl-C stops it.")
        else:
            _say(
                "Tunnel process is running but the server cannot reach the "
                "proxy yet. Leaving it up; Ctrl-C stops it."
            )
            if bind_host != "127.0.0.1":
                _proxy_gatewayports_note(target, bind_host)
        process.wait()
        _warn(f"frisket: ssh exited with code {process.returncode}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        _say("Tunnel stopped.")
        return EXIT_OK
    finally:
        # Every exit path — clean, interrupted, or an unexpected error —
        # must stop the ssh child and clear the server setting; a surviving
        # tunnel would hold the remote port hostage for the next `up`.
        _stop_ssh(process)
        _clear_media_proxy_after_run(client, name)


def _stop_ssh(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def _clear_media_proxy_after_run(client: RemoteClient, name: str) -> None:
    try:
        client.media_proxy_clear()
    except RemoteError as exc:
        _warn(
            f"frisket: could not clear the media proxy on '{name}' ({exc}); "
            "run `frisket proxy down` once the server is reachable"
        )
    else:
        _say(f"Media proxy cleared on '{name}'.")


def _proxy_status(argv: list[str]) -> int:
    parser = _Parser(prog="frisket proxy status")
    _server_argument(parser)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    name, _entry, client = _client_for(args.server)
    setting = client.media_proxy_get()
    check = client.media_proxy_check() if setting.get("configured") else None
    if args.as_json:
        _say(
            json.dumps(
                {"server": name, "setting": setting, "check": check},
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_OK
    if not setting.get("configured"):
        _say(f"No media proxy is set on '{name}'.")
        if setting.get("env_invalid"):
            _warn(
                "frisket: the server environment sets FRISKET_MEDIA_PROXY but "
                f"the value is being ignored: {setting['env_invalid']}"
            )
        return EXIT_OK
    source = "server environment" if setting.get("source") == "env" else "admin API"
    _say(f"Media proxy on '{name}': {setting.get('url')} (set via {source})")
    _proxy_report_check(check or {})
    return EXIT_OK


def _proxy_down(argv: list[str]) -> int:
    parser = _Parser(prog="frisket proxy down")
    _server_argument(parser)
    args = parser.parse_args(argv)
    name, _entry, client = _client_for(args.server)
    payload = client.media_proxy_clear()
    if payload.get("cleared"):
        _say(f"Media proxy cleared on '{name}'.")
    else:
        _say(f"No admin-set media proxy on '{name}'; nothing to clear.")
    if payload.get("configured") and payload.get("source") == "env":
        _warn(
            "frisket: the server environment still sets a media proxy "
            "(FRISKET_MEDIA_PROXY); only the operator can unset that"
        )
    return EXIT_OK


_PROXY_VERBS = {
    "up": _proxy_up,
    "status": _proxy_status,
    "down": _proxy_down,
}


def proxy_command(argv: list[str]) -> int:
    def run(rest: list[str]) -> int:
        if not rest or rest[0] in {"-h", "--help"}:
            _say(
                "Usage: frisket proxy up [--ssh USER@HOST] [--port N] "
                "[--bind ADDR] [--run] [--server ...]\n"
                "       frisket proxy status [--json] [--server ...]\n"
                "       frisket proxy down [--server ...]\n"
                "\n"
                "Routes the server's media downloads through your own computer: "
                "`up` points the server at a loopback SOCKS proxy and prints "
                "(or with --run, starts) the reverse ssh tunnel that provides it."
            )
            return EXIT_OK if rest else EXIT_VALIDATION
        verb = _PROXY_VERBS.get(rest[0])
        if verb is None:
            raise UsageError(f"unknown proxy subcommand '{rest[0]}'")
        return verb(rest[1:])

    return _dispatch(run, argv)


__all__ = [
    "proxy_command",
    "remote_command",
    "secrets_command",
    "token_command",
    "users_command",
]
