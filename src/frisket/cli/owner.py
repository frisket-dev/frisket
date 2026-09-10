"""`frisket owner ...` — narrow local-console recovery for the sole server owner."""

import argparse
import os
import sys
from pathlib import Path

from frisket.cli._shared import _standalone_database_url


def owner_admin(argv: list[str]) -> int:
    """Narrow local-console recovery for the sole server owner."""

    parser = argparse.ArgumentParser(prog="frisket owner")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "reset-password",
        help="prompt for a new password for the sole owner and revoke sessions",
    )
    args = parser.parse_args(argv)
    if args.command != "reset-password":  # argparse owns the reachable cases
        return 2

    database_url = os.environ.get("FRISKET_TEAM_DATABASE_URL", "").strip()
    if not database_url:
        data_dir = Path(os.environ.get("FRISKET_DATA_DIR", "/data")).expanduser()
        standalone_database = data_dir.resolve() / "server.sqlite3"
        if standalone_database.is_file():
            database_url = _standalone_database_url(data_dir.resolve())
    if not database_url:
        print(
            "frisket owner: no server control database was found",
            file=sys.stderr,
        )
        return 2

    import getpass

    def read_password(prompt: str) -> str:
        if sys.stdin.isatty():
            return getpass.getpass(prompt)
        value = sys.stdin.readline()
        if value == "":
            raise EOFError
        return value.rstrip("\r\n")

    try:
        password = read_password("New password: ")
        confirmation = read_password("Confirm new password: ")
    except (EOFError, KeyboardInterrupt):
        print("frisket owner: password input cancelled", file=sys.stderr)
        return 1
    if password != confirmation:
        print("frisket owner: password confirmation does not match", file=sys.stderr)
        return 1

    import sqlalchemy as sa

    from frisket.team.local_auth import SetupError, reset_sole_owner_password
    from frisket.team.team_bootstrap import preflight_team_schema

    engine = sa.create_engine(database_url, future=True)
    try:
        preflight_team_schema(engine)
        email = reset_sole_owner_password(engine, password=password)
    except SetupError as exc:
        print(f"frisket owner: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - never print a DSN or driver message
        print("frisket owner: password reset failed", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(f"Password reset for {email}; existing sessions were revoked.")
    return 0
