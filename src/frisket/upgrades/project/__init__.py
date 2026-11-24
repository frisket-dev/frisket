"""The single project-upgrade boundary; individual converters are private."""

from __future__ import annotations

from frisket.engine.store.schema import BundleSchemaMismatch, require_current_schema
from frisket.upgrades.project.network_policy_migration import upgrade_network_policy
from frisket.upgrades.project.usage_data_sharing_migration import (
    upgrade_usage_data_sharing,
)


def upgrade_project(project, *, project_id=None, recipes=None) -> None:
    """Bring stored project data and available workspace recipes up to date.

    The caller holds its normal project-open lock. ``recipes`` is a lazy reader:
    standalone projects need no workspace, and converted layouts skip the read.
    Current projects never load or traverse historical schema converters.
    """
    try:
        require_current_schema(project.db, bundle_path=project.path)
    except BundleSchemaMismatch:
        from .chain import upgrade_project_schema

        upgrade_project_schema(project.db, bundle_path=project.db_path)
        require_current_schema(project.db, bundle_path=project.path)
    if project.get_meta("usage_data_sharing") is None:
        upgrade_usage_data_sharing(project)
    if project.get_meta("network_policy") is not None:
        upgrade_network_policy(project)
    if recipes is not None:
        from .extraction_recipes import (
            LEGACY_EXTRACTION_LAYOUTS_META_KEY,
            LEGACY_EXTRACTION_LAYOUTS_VERSION,
            upgrade_legacy_extraction_layouts,
        )

        if project_id is None:
            raise ValueError("Workspace recipe conversion requires a project ID")
        if (
            project.get_meta(LEGACY_EXTRACTION_LAYOUTS_META_KEY)
            != LEGACY_EXTRACTION_LAYOUTS_VERSION
        ):
            upgrade_legacy_extraction_layouts(project, project_id, recipes())
