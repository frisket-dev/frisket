// Shared project-role predicates. One mint site for "what can this role do"
// so the UI's read of the viewer/reviewer/editor/owner ladder cannot drift
// from itself across the settings screen, the top nav export menus, and
// wherever else a control needs to match the backend's endpoint_catalog
// tier (frisket.contracts.http.endpoint_catalog) for the same action.
import type { ProjectInfo, ProjectRole } from './types';

export function projectRole(project?: ProjectInfo | null): ProjectRole {
  return project?.role ?? 'owner';
}

export function canOwnProject(project?: ProjectInfo | null): boolean {
  return projectRole(project) === 'owner';
}

export function canEditProject(project?: ProjectInfo | null): boolean {
  return ['editor', 'owner'].includes(projectRole(project));
}

/** Bulk data-takeout routes (whole-project export, sheet CSV export, work
 *  log export, embedding index export download/manifest) are reviewer-tier
 *  on the backend, one rung above the ordinary per-cell/per-row reads a
 *  viewer gets: a newsroom granting read access does not mean to grant a
 *  full data takeout. */
export function canReviewProject(project?: ProjectInfo | null): boolean {
  return ['reviewer', 'editor', 'owner'].includes(projectRole(project));
}
