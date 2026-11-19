"""Store a placeholder provider key in one project of a browser-suite workspace.

Usage:
    python scripts/e2e/seed_provider_key.py <workspace> <project id> <provider>

Saving a key through Settings needs a validation token from a live provider
probe, which the browser suite never makes. This writes the project key with
the production store function instead, so a run can price a model the
project would otherwise route to Cloud. The value is a fixed placeholder no
provider accepts.

Only a workspace the browser-suite harness prepared carries the active
marker (web/e2e-workspace-cleanup.mjs, prepareE2EWorkspace); any other
workspace is refused before it is opened.
"""

from __future__ import annotations

import sys
from pathlib import Path

from frisket.engine.store import Project
from frisket.team.security.secrets import encrypt_secret, key_hint

E2E_ACTIVE_MARKER = ".frisket-e2e-active.json"
PLACEHOLDER_KEY = "e2e-placeholder-not-a-provider-key"


class NotAnE2EWorkspace(RuntimeError):
    """The workspace was not prepared by the browser-suite harness."""


def seed_placeholder_provider_key(
    workspace: Path, project_id: str, provider: str
) -> None:
    if not (workspace / E2E_ACTIVE_MARKER).is_file():
        raise NotAnE2EWorkspace(
            f"{workspace} has no {E2E_ACTIVE_MARKER}; placeholder provider keys "
            "are written only into a browser-suite workspace"
        )
    project_path = workspace / f"{project_id}.frisket"
    if not project_path.is_dir():
        raise FileNotFoundError(f"no project {project_id!r} in {workspace}")
    project = Project(project_path)
    try:
        with project.transaction_scope():
            project.set_provider_key(
                provider=provider,
                encrypted=encrypt_secret(PLACEHOLDER_KEY),
                hint=key_hint(PLACEHOLDER_KEY),
                spend_cap_micro=None,
            )
    finally:
        project.close()


def main() -> None:
    workspace, project_id, provider = sys.argv[1:]
    seed_placeholder_provider_key(Path(workspace), project_id, provider)


if __name__ == "__main__":
    main()
