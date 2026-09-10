"""App runtime environment promotion commands."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from frisket.operability.control.env_manifest import ManifestError, names_for_surface
from frisket.operability.control.env_values import (
    EnvValueError,
    value_for_name,
    write_secret_file,
)


DEFAULT_ENV_FILE = ".secrets/frisket.env"
DEFAULT_REMOTE_ENV = "/opt/frisket/compose/.env"
VALUE_FLAGS = {
    "--confirm-overwrite",
    "--env-file",
    "--remote",
    "--remote-env",
    "--name",
}

REMOTE_APP_ENV_SCRIPT = r"""
from datetime import datetime, timezone
from pathlib import Path
import re
import sys

env_path = Path(sys.argv[1])
fragment_path = Path(sys.argv[2])
apply_mode = sys.argv[3] == '1'
confirm_count = int(sys.argv[4])
confirmed_overwrites = set(sys.argv[5:5 + confirm_count])
updates = {}
fragment_text = fragment_path.read_text().strip()
if fragment_text:
    import json
    parsed = json.loads(fragment_text)
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in parsed.items()
    ):
        print('invalid app env fragment', file=sys.stderr)
        raise SystemExit(2)
    updates.update(parsed)

ASSIGNMENT_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


class EnvParseError(ValueError):
    pass


def parse_double_quoted(raw, line_no):
    out = []
    escaped = False
    for index, char in enumerate(raw[1:], start=1):
        if escaped:
            out.append(
                {
                    'n': chr(10),
                    'r': chr(13),
                    't': chr(9),
                    '"': '"',
                    '\\': '\\',
                    '$': '$',
                }.get(char, char)
            )
            escaped = False
            continue
        if char == '\\':
            escaped = True
            continue
        if char == '"':
            return ''.join(out), raw[index + 1:]
        out.append(char)
    raise EnvParseError(f'invalid remote env line {line_no}: unterminated double quote')


def parse_single_quoted(raw, line_no):
    end = raw.find("'", 1)
    if end == -1:
        raise EnvParseError(f'invalid remote env line {line_no}: unterminated single quote')
    return raw[1:end], raw[end + 1:]


def parse_unquoted(raw):
    for index, char in enumerate(raw):
        if char == '#' and (index == 0 or raw[index - 1].isspace()):
            raw = raw[:index]
            break
    return raw.rstrip()


def parse_remote_assignment(line, line_no):
    stripped = line.lstrip()
    if not stripped or stripped.startswith('#'):
        return None
    match = ASSIGNMENT_RE.match(stripped)
    if not match:
        raise EnvParseError(f'invalid remote env line {line_no}: expected NAME=VALUE')
    raw_value = match.group(2).lstrip()
    if not raw_value:
        return match.group(1), ''
    if raw_value.startswith("'"):
        value, tail = parse_single_quoted(raw_value, line_no)
    elif raw_value.startswith('"'):
        value, tail = parse_double_quoted(raw_value, line_no)
    else:
        return match.group(1), parse_unquoted(raw_value)
    tail = tail.strip()
    if tail and not tail.startswith('#'):
        raise EnvParseError(f'invalid remote env line {line_no}: trailing data after quote')
    return match.group(1), value


existing = {}
source_lines = env_path.read_text().splitlines() if env_path.exists() else []
try:
    for line_no, line in enumerate(source_lines, start=1):
        parsed_assignment = parse_remote_assignment(line, line_no)
        if parsed_assignment is None:
            continue
        key, value = parsed_assignment
        existing[key] = value
except EnvParseError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(2)

added = sorted(key for key in updates if key not in existing)
updated = sorted(
    key for key, value in updates.items()
    if key in existing and existing[key] != value
)
unchanged = sorted(
    key for key, value in updates.items()
    if key in existing and existing[key] == value
)

if not apply_mode:
    if updated:
        print('remote app env differs: ' + ', '.join(updated))
    if added:
        print('remote app env missing: ' + ', '.join(added))
    if unchanged:
        print('remote app env same: ' + ', '.join(unchanged))
    if not added and not updated and not unchanged:
        print('remote app env has no selected names')
    unconfirmed = sorted(name for name in updated if name not in confirmed_overwrites)
    if unconfirmed:
        print(
            'refusing remote app env differs without confirmation: '
            + ', '.join(unconfirmed),
            file=sys.stderr,
        )
        raise SystemExit(3)
    raise SystemExit(0)

unconfirmed = sorted(name for name in updated if name not in confirmed_overwrites)
if unconfirmed:
    print(
        'refusing overwrite for existing app env name(s): ' + ', '.join(unconfirmed),
        file=sys.stderr,
    )
    raise SystemExit(3)

if not added and not updated:
    if unchanged:
        print('unchanged app env name(s): ' + ', '.join(unchanged))
    else:
        print('no app env changes requested')
    raise SystemExit(0)

seen = set()
out = []

def quote_env_value(value):
    double_quote = chr(34)
    backslash = chr(92)
    if value == '' or any(char.isspace() for char in value) or ' #' in value or value.startswith('#') or value.startswith(double_quote) or value.startswith("'"):
        escaped = (
            value
            .replace(backslash, backslash + backslash)
            .replace(double_quote, backslash + double_quote)
            .replace(chr(10), backslash + 'n')
            .replace(chr(13), backslash + 'r')
        )
        return double_quote + escaped + double_quote
    return value

for line in source_lines:
    if not line or line.lstrip().startswith('#') or '=' not in line:
        out.append(line)
        continue
    parsed_assignment = parse_remote_assignment(line, 0)
    if parsed_assignment is None:
        out.append(line)
        continue
    key, _ = parsed_assignment
    if key in updates:
        out.append(f'{key}={quote_env_value(updates[key])}')
        seen.add(key)
    else:
        out.append(line)
for key in sorted(set(updates) - seen):
    out.append(f'{key}={quote_env_value(updates[key])}')

if env_path.exists():
    backup_path = env_path.with_name(
        env_path.name + '.bak-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    )
    backup_path.write_text('\n'.join(source_lines) + ('\n' if source_lines else ''))
env_path.write_text('\n'.join(out) + '\n')
if updated:
    print('updated app env name(s): ' + ', '.join(updated))
if added:
    print('added app env name(s): ' + ', '.join(added))
if unchanged:
    print('unchanged app env name(s): ' + ', '.join(unchanged))
"""


def _parse_promote_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="frisket-control env promote",
        description=(
            "Promote only allowlisted app-runtime variables to the server compose .env."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false", default=False)
    mode.add_argument("--apply", dest="apply", action="store_true")
    parser.add_argument("--check-remote", action="store_true")
    parser.add_argument("--all-allowlisted", action="store_true")
    parser.add_argument("--confirm-overwrite", action="append", default=[])
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument(
        "--from-environment",
        action="store_true",
        help="read selected values from this process environment instead of the env file",
    )
    parser.add_argument(
        "--remote",
        default=os.environ.get("FRISKET_APP_REMOTE"),
        help="SSH target for the app host, e.g. user@host"
        " (defaults to FRISKET_APP_REMOTE)",
    )
    parser.add_argument("--remote-env", default=DEFAULT_REMOTE_ENV)
    parser.add_argument("--name", action="append", default=[])
    return parser.parse_args(argv)


def _missing_value_error(argv: list[str]) -> str | None:
    for index, arg in enumerate(argv):
        if arg in VALUE_FLAGS:
            if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
                return f"{arg} requires a value"
    return None


def _source_label(*, from_environment: bool, env_file: str) -> str:
    return "environment" if from_environment else env_file


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, check=False, **kwargs)


def _cleanup_remote_tmp(remote: str, remote_tmp: str) -> None:
    if not remote_tmp:
        return
    subprocess.run(
        ["ssh", remote, f"rm -rf {shlex.quote(remote_tmp)}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )


def promote(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if error := _missing_value_error(raw_args):
        print(error, file=sys.stderr)
        return 2
    args = _parse_promote_args(raw_args)
    if not args.remote:
        print(
            "--remote is required (or set FRISKET_APP_REMOTE), e.g. user@host",
            file=sys.stderr,
        )
        return 2
    try:
        allowed_names = names_for_surface("app-runtime")
    except ManifestError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    selected_names = list(args.name)
    if args.apply and not selected_names and not args.all_allowlisted:
        print(
            "--apply requires at least one --name or explicit --all-allowlisted",
            file=sys.stderr,
        )
        return 2
    if not selected_names:
        selected_names = allowed_names

    env_path = Path(args.env_file)
    if not args.from_environment and not env_path.is_file():
        print(f"env file not found: {args.env_file}", file=sys.stderr)
        return 2

    allowed = set(allowed_names)
    updates: dict[str, str] = {}
    for name in selected_names:
        if name not in allowed:
            print(f"refusing non-allowlisted app env var: {name}", file=sys.stderr)
            return 2
        try:
            value = value_for_name(
                env_path,
                name,
                from_environment=args.from_environment,
            )
        except EnvValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if value is None:
            source = _source_label(
                from_environment=args.from_environment,
                env_file=args.env_file,
            )
            print(f"skip {name}: missing or empty in {source}")
            continue
        if "\n" in value or "\r" in value:
            print(
                f"{name} contains a newline and cannot be written to an env fragment",
                file=sys.stderr,
            )
            return 2
        updates[name] = value
        action = "promote" if args.apply else "dry-run"
        print(f"{action} {name} to {args.remote}:{args.remote_env}")

    for name in args.confirm_overwrite:
        if name not in allowed:
            print(
                f"refusing non-allowlisted overwrite confirmation: {name}",
                file=sys.stderr,
            )
            return 2

    if not updates:
        print("no app-runtime values to promote")
        return 0

    if not args.apply and not args.check_remote:
        print(
            f"dry run only; rerun with --apply to update {args.remote}:{args.remote_env}"
        )
        return 0

    if shutil.which("scp") is None:
        print("scp is required for remote checks/apply", file=sys.stderr)
        return 2
    if shutil.which("ssh") is None:
        print("ssh is required for remote checks/apply", file=sys.stderr)
        return 2

    fd, fragment_name = tempfile.mkstemp(prefix="frisket-app-env-", text=True)
    os.close(fd)
    fragment = Path(fragment_name)
    remote_tmp = ""
    try:
        os.chmod(fragment, 0o600)
        write_secret_file(fragment, json.dumps(updates, sort_keys=True))
        mktemp = _run(
            ["ssh", args.remote, "umask 077; mktemp -d /tmp/frisket-env.XXXXXX"],
            capture_output=True,
        )
        if mktemp.returncode != 0:
            if mktemp.stderr:
                print(mktemp.stderr, end="", file=sys.stderr)
            return mktemp.returncode
        remote_tmp = mktemp.stdout.strip()
        if not remote_tmp:
            print("remote temp directory was not created", file=sys.stderr)
            return 2
        remote_fragment = f"{remote_tmp}/fragment"
        scp = _run(["scp", "-q", str(fragment), f"{args.remote}:{remote_fragment}"])
        if scp.returncode != 0:
            return scp.returncode

        apply_flag = "1" if args.apply else "0"
        remote_env_q = shlex.quote(args.remote_env)
        remote_tmp_q = shlex.quote(remote_tmp)
        remote_python_argv = [
            "python3",
            "-",
            args.remote_env,
            remote_fragment,
            apply_flag,
            str(len(args.confirm_overwrite)),
            *args.confirm_overwrite,
        ]
        remote_python_cmd = " ".join(shlex.quote(item) for item in remote_python_argv)
        remote_command = (
            "set -euo pipefail; umask 077; "
            f"trap 'rm -rf {remote_tmp_q}' EXIT; "
            f"{remote_python_cmd}; "
            f"if [[ {apply_flag} -eq 1 ]]; then chmod 600 {remote_env_q}; fi"
        )
        remote = subprocess.run(
            ["ssh", args.remote, remote_command],
            input=REMOTE_APP_ENV_SCRIPT,
            text=True,
            check=False,
        )
        if remote.returncode != 0:
            return remote.returncode
        remote_tmp = ""
        if args.apply:
            print(
                "updated "
                f"{args.remote}:{args.remote_env} with allowlisted app-runtime variables"
            )
        else:
            print(
                "remote check only; rerun with --apply to update "
                f"{args.remote}:{args.remote_env}"
            )
        return 0
    finally:
        try:
            fragment.unlink()
        except FileNotFoundError:
            pass
        _cleanup_remote_tmp(args.remote, remote_tmp)
