"""Public self-host release verification entrypoint."""

from __future__ import annotations

import argparse
import sys

TOKEN_USAGE = """Usage:
  frisket-control token mint [--label LABEL]

Mints an operator token against this host's server control database and
prints the raw token ONCE (stdout). Only its hash is stored; pair it from a
workstation with: frisket remote link <server-url> --token <printed-token>
"""


def _token(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(TOKEN_USAGE)
        return 0 if argv else 2
    if argv[0] != "mint":
        print(f"frisket-control token: unknown subcommand '{argv[0]}'", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(prog="frisket-control token mint")
    parser.add_argument("--label", default=None, help="display label for this token")
    args = parser.parse_args(argv[1:])

    from frisket.team.operator_service import (
        OperatorTokenError,
        mint_operator_token,
        resolve_control_database_url,
    )

    database_url = resolve_control_database_url()
    if not database_url:
        print(
            "frisket-control token: no server control database was found "
            "(set FRISKET_TEAM_DATABASE_URL or FRISKET_DATA_DIR)",
            file=sys.stderr,
        )
        return 2

    import sqlalchemy as sa

    from frisket.team.bootstrap import PreSplitSchemaError
    from frisket.team.schema import orgs
    from frisket.team.team_bootstrap import preflight_team_schema

    engine = sa.create_engine(database_url, future=True)
    try:
        preflight_team_schema(engine)
        with engine.connect() as cx:
            org_id = cx.execute(
                sa.select(orgs.c.id).order_by(orgs.c.id).limit(1)
            ).scalar_one_or_none()
        if org_id is None:
            print(
                "frisket-control token: the server has not initialized its "
                "control database yet; start the server first",
                file=sys.stderr,
            )
            return 1
        raw, token_id = mint_operator_token(
            engine, org_id=int(org_id), label=args.label
        )
    except OperatorTokenError as exc:
        print(f"frisket-control token: {exc}", file=sys.stderr)
        return 3
    except PreSplitSchemaError as exc:
        print(f"frisket-control token: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - never echo a DSN or driver message
        print("frisket-control token: minting failed", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    label_note = f" label={args.label}" if args.label else ""
    print(
        f"frisket-control token: minted operator token id={token_id}{label_note} "
        "(hash stored; the raw value below is shown ONCE)",
        file=sys.stderr,
    )
    print(raw)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "token":
        return _token(argv[1:])
    print("Use frisket doctor and the documented self-host release checks.")
    return 0
