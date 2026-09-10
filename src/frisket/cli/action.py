"""`frisket action ...` — canonical action requests (schema/validate/run)."""

import json
import sys
from pathlib import Path


def action(argv: list[str]) -> int:
    """Expose the canonical action catalog, validation, and project runner."""
    import argparse

    ap = argparse.ArgumentParser(prog="frisket action")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("schema", help="print the action request catalog")
    validate = sub.add_parser("validate", help="validate an action request JSON file")
    validate.add_argument(
        "spec", help="path to a JSON action request, or '-' for stdin"
    )
    run = sub.add_parser("run", help="run an action request JSON file")
    run.add_argument("--project", required=True, help="path to a .frisket bundle")
    run.add_argument("--project-id", default=None)
    run.add_argument("spec", help="path to a JSON action request, or '-' for stdin")
    args = ap.parse_args(argv)

    from frisket.contracts.action import (
        ActionError,
        ActionValidationResult,
    )
    from frisket.actions.system import root_action_catalog, validate_root_action

    if args.command == "schema":
        print(json.dumps(root_action_catalog().model_dump(mode="json"), sort_keys=True))
        return 0

    try:
        if args.spec == "-":
            data_bytes = sys.stdin.buffer.read()
        else:
            spec_path = Path(args.spec)
            data_bytes = spec_path.read_bytes()
        raw = data_bytes.decode("utf-8")
    except OSError as exc:
        result = ActionValidationResult(
            ok=False,
            error=ActionError(
                code="invalid_input",
                message=str(exc),
                field="spec",
            ),
        )
        print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
        return 2
    except (UnicodeDecodeError, ValueError) as exc:
        result = ActionValidationResult(
            ok=False,
            error=ActionError(
                code="invalid_input",
                message=str(exc),
                field="spec",
            ),
        )
        print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
        return 2

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        result = ActionValidationResult(
            ok=False,
            error=ActionError(
                code="invalid_json",
                message=str(exc),
                field="spec",
            ),
        )
        print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
        return 2

    if args.command == "run":
        from frisket.authoring.plugin_registry import default_registry
        from frisket.engine.executor import run_action_spec
        from frisket.engine.store import Project

        default_registry()
        project_path = Path(args.project)
        if not project_path.exists() or not (project_path / "project.db").exists():
            result = ActionValidationResult(
                ok=False,
                error=ActionError(
                    code="invalid_project",
                    message=f"not a frisket project bundle: {project_path}",
                    field="project",
                ),
            )
            print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
            return 2
        project = Project(project_path)
        try:
            project_id = args.project_id or project_path.stem
            action_result = run_action_spec(
                project,
                data,
                project_id=project_id,
            )
        finally:
            project.close()
        print(json.dumps(action_result.model_dump(mode="json"), sort_keys=True))
        if action_result.errors:
            for err in action_result.errors[:5]:
                print(f"error: {err.code}: {err.message}", file=sys.stderr)
            if len(action_result.errors) > 5:
                print(
                    f"error: {len(action_result.errors) - 5} more error(s)",
                    file=sys.stderr,
                )
        return 0 if action_result.status == "completed" else 1

    result = validate_root_action(data)
    print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
    if not result.ok and result.error is not None:
        print(
            f"error: {result.error.code}: {result.error.message}",
            file=sys.stderr,
        )
    return 0 if result.ok else 1
