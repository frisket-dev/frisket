"""`frisket queue ...` — provision and migrate the hosted run queue."""

import argparse
import os
import sys


def queue_admin(argv: list[str]) -> int:
    """Provision or migrate the privilege-separated Postgres run queue."""
    from frisket.engine.jobs.queue_migrations import (
        QueueSchemaError,
        migrate_run_queue_schema,
    )
    from frisket.engine.jobs.queue_provision import (
        QueueProvisionError,
        provision_run_queue_database,
    )

    parser = argparse.ArgumentParser(prog="frisket queue")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("provision", help="provision fixed queue roles/database")
    commands.add_parser("migrate", help="apply ordered queue schema migrations")
    args = parser.parse_args(argv)

    try:
        if args.command == "provision":
            provision_run_queue_database(
                admin_url=os.environ.get("FRISKET_DATABASE_ADMIN_URL", ""),
                runtime_url=os.environ.get("FRISKET_RUN_QUEUE_DATABASE_URL", ""),
            )
        else:
            migrate_run_queue_schema(
                admin_url=os.environ.get("FRISKET_DATABASE_ADMIN_URL", ""),
                runtime_url=os.environ.get("FRISKET_RUN_QUEUE_DATABASE_URL", ""),
            )
    except (QueueProvisionError, QueueSchemaError) as exc:
        message = str(exc)
        print(f"frisket queue: {message}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - never echo driver messages or locators
        message = "run-queue administration failed"
        print(f"frisket queue: {message}", file=sys.stderr)
        return 1
    return 0
