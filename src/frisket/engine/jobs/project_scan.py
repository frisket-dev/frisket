"""Scheduler scans over the project bundles under one workspace root."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path

from frisket.engine.store import Project
from frisket.project_identity import ProjectStorageKey

from .project_opener import ProjectOpener, require_opener_storage_organization_id

LOG = logging.getLogger(__name__)


def bundle_opener(
    direct: Callable[[Path], Project],
    project_opener: ProjectOpener | None,
    storage_org_id: int | None,
) -> Callable[[Path], Project]:
    """How a scan opens each bundle: ``direct``, or through the injected opener
    keyed by the root's DECLARED storage organization (never the directory name). A scan
    that injects an opener refuses outright when that identity is missing."""
    if project_opener is None:
        return direct
    org_id = require_opener_storage_organization_id(storage_org_id)

    def open_bundle(bundle: Path) -> Project:
        return project_opener(
            ProjectStorageKey(storage_org_id=org_id, project_slug=bundle.stem), bundle
        )

    return open_bundle


def scan_projects(
    bundles: Iterable[Path],
    open_bundle: Callable[[Path], Project],
    visit: Callable[[str, Project], None],
    *,
    failure_event: str,
) -> None:
    """Open each bundle and pass ``(project_id, project)`` to ``visit``.

    A project that fails to open or to scan is logged and skipped, so one
    unreadable bundle cannot stop the scan of the others; whatever was opened
    is closed either way.
    """
    for bundle in bundles:
        project = None
        try:
            project = open_bundle(bundle)
            visit(bundle.stem, project)
        except Exception as exc:
            LOG.warning(
                failure_event,
                extra={
                    "event": failure_event,
                    "project_id": bundle.stem,
                    "project_path": str(bundle),
                    "error": str(exc),
                },
                exc_info=True,
            )
        finally:
            if project is not None:
                project.close()
