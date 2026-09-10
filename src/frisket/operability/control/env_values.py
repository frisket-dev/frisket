"""Read secret values from a dotenv file without shell string parsing."""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ASSIGNMENT_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


class EnvValueError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedValue:
    value: str
    file_backed: bool = False


def _parse_double_quoted(raw: str, line_no: int) -> tuple[str, str]:
    out: list[str] = []
    escaped = False
    for index, char in enumerate(raw[1:], start=1):
        if escaped:
            out.append(
                {
                    "n": "\n",
                    "r": "\r",
                    "t": "\t",
                    '"': '"',
                    "\\": "\\",
                    "$": "$",
                }.get(char, char)
            )
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            return "".join(out), raw[index + 1 :]
        out.append(char)
    raise EnvValueError(f"invalid env file line {line_no}: unterminated double quote")


def _parse_single_quoted(raw: str, line_no: int) -> tuple[str, str]:
    end = raw.find("'", 1)
    if end == -1:
        raise EnvValueError(
            f"invalid env file line {line_no}: unterminated single quote"
        )
    return raw[1:end], raw[end + 1 :]


def _parse_unquoted(raw: str) -> str:
    for index, char in enumerate(raw):
        if char == "#" and (index == 0 or raw[index - 1].isspace()):
            raw = raw[:index]
            break
    return raw.rstrip()


def _parse_value(raw: str, line_no: int) -> ParsedValue:
    raw = raw.lstrip()
    if not raw:
        return ParsedValue("")
    if raw.startswith("'"):
        value, tail = _parse_single_quoted(raw, line_no)
        file_backed = False
    elif raw.startswith('"'):
        value, tail = _parse_double_quoted(raw, line_no)
        file_backed = False
    else:
        value = _parse_unquoted(raw)
        return ParsedValue(value, file_backed=value.startswith("@") and len(value) > 1)
    tail = tail.strip()
    if tail and not tail.startswith("#"):
        raise EnvValueError(
            f"invalid env file line {line_no}: trailing data after quote"
        )
    return ParsedValue(value, file_backed=file_backed)


def parse_env_file(path: Path) -> dict[str, ParsedValue]:
    if not path.exists():
        raise EnvValueError(f"env file not found: {path}")
    values: dict[str, ParsedValue] = {}
    try:
        text = path.read_text()
    except OSError as exc:
        raise EnvValueError(f"env file could not be read: {path}") from exc
    if "\x00" in text:
        raise EnvValueError("env file contains NUL bytes")
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.lstrip()
        if not line or line.startswith("#"):
            continue
        match = ASSIGNMENT_RE.match(line)
        if not match:
            raise EnvValueError(f"invalid env file line {line_no}: expected NAME=VALUE")
        name = match.group(1)
        values[name] = _parse_value(match.group(2), line_no)
    return values


def _resolve_file_backed_value(env_file: Path, name: str, parsed: ParsedValue) -> str:
    if not parsed.file_backed:
        return parsed.value
    target = Path(parsed.value[1:]).expanduser()
    if not target.is_absolute():
        target = env_file.parent / target
    if not target.is_file():
        raise EnvValueError(f"file-backed value for {name} could not be read")
    try:
        value = target.read_text()
    except OSError as exc:
        raise EnvValueError(f"file-backed value for {name} could not be read") from exc
    if "\x00" in value:
        raise EnvValueError(f"file-backed value for {name} contains NUL bytes")
    return value


def value_for_name(
    env_file: Path, name: str, *, from_environment: bool = False
) -> str | None:
    if not ENV_NAME_RE.match(name):
        raise EnvValueError(f"invalid env name: {name}")
    if from_environment:
        value = os.environ.get(name)
        if value is None or value == "":
            return None
        if "\x00" in value:
            raise EnvValueError(f"environment value for {name} contains NUL bytes")
        return value
    values = parse_env_file(env_file)
    if name not in values:
        return None
    value = _resolve_file_backed_value(env_file, name, values[name])
    if value == "":
        return None
    return value


def write_secret_file(path: Path, value: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        if nofollow and exc.errno == errno.ELOOP:
            raise EnvValueError(f"output path is a symlink: {path}") from exc
        raise EnvValueError(f"output path could not be written: {path}") from exc
    with os.fdopen(fd, "w") as handle:
        handle.write(value)
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        raise EnvValueError(f"output path mode could not be set: {path}") from exc


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    get = subparsers.add_parser("get", help="read one value")
    get.add_argument("--env-file", required=True)
    get.add_argument(
        "--from-environment",
        action="store_true",
        help="read the selected name from the process environment instead of the env file",
    )
    get.add_argument("--name", required=True)
    get.add_argument("--output", required=True)

    fragment = subparsers.add_parser("append-json", help="append NAME/VALUE to JSON")
    fragment.add_argument("--env-file", required=True)
    fragment.add_argument(
        "--from-environment",
        action="store_true",
        help="read the selected name from the process environment instead of the env file",
    )
    fragment.add_argument("--name", required=True)
    fragment.add_argument("--output", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        value = value_for_name(
            Path(args.env_file),
            args.name,
            from_environment=args.from_environment,
        )
        if value is None:
            return 1
        if args.command == "get":
            write_secret_file(Path(args.output), value)
            return 0
        if "\n" in value or "\r" in value:
            raise EnvValueError(
                f"{args.name} contains a newline and cannot be written to an env fragment"
            )
        output_path = Path(args.output)
        if output_path.exists() and output_path.read_text().strip():
            try:
                values = json.loads(output_path.read_text())
            except json.JSONDecodeError as exc:
                raise EnvValueError(f"fragment JSON is invalid: {output_path}") from exc
            if not isinstance(values, dict) or not all(
                isinstance(key, str) and isinstance(item, str)
                for key, item in values.items()
            ):
                raise EnvValueError(f"fragment JSON must be an object: {output_path}")
        else:
            values = {}
        values[args.name] = value
        write_secret_file(output_path, json.dumps(values, sort_keys=True))
        return 0
    except EnvValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
