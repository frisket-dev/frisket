import type { WorkbenchHostId, WorkbenchPlacementMode } from './descriptors';
import type { WorkbenchResolvedLayoutContribution } from './layout';

const GRID_CONTRIBUTION_ID = 'frisket.core.view.grid';

export interface WorkbenchVisibilityHostPolicy {
  host: WorkbenchHostId;
  mode: WorkbenchPlacementMode;
  locationId: string;
  locationLabel: string;
  menuSectionTitle: string;
  // Anchors the user must always be able to reach: excluded from visibility
  // targets entirely, and preset snapshots never persist them as hidden.
  nonHideableContributionIds: readonly string[];
  // First-party contributions that participate in visibility for this host.
  // Runtime-index (plugin) contributions always participate.
  firstPartyVisibilityTargetIds: readonly string[];
}

// The per-host visibility policy. Each placement-parity cohort that makes a
// host user-manageable adds a row here; hosts without a row have no visibility
// affordance (their contributions never appear in the Window menu).
const WORKBENCH_VISIBILITY_HOST_POLICIES: readonly WorkbenchVisibilityHostPolicy[] = [
  {
    host: 'mainView',
    mode: 'pane',
    locationId: 'mainView',
    locationLabel: 'Main View',
    menuSectionTitle: 'Main View Panels',
    nonHideableContributionIds: [GRID_CONTRIBUTION_ID],
    firstPartyVisibilityTargetIds: [
      'frisket.core.view.evidence',
      'frisket.core.view.source_health',
      'frisket.geo.view.map',
      'frisket.investigative.view.graph_neighborhood',
      'frisket.media.view.image_gallery',
    ],
  },
  {
    host: 'bottomDock',
    mode: 'tab',
    locationId: 'bottomDock',
    locationLabel: 'Bottom Dock',
    menuSectionTitle: 'Bottom Dock Tabs',
    // Jobs is the dock's active-tab fallback anchor — the only non-hideable
    // bottomDock target. The plugin manager tab retired: Settings hosts the
    // full manager now, always reachable there regardless of dock
    // visibility state, so it no longer needs a dock-visibility carve-out.
    nonHideableContributionIds: [
      'frisket.core.panel.jobs',
    ],
    firstPartyVisibilityTargetIds: [
      'frisket.core.panel.errors',
      'frisket.core.panel.history',
      // preview retired from the dock: its content was a static signpost that
      // never rendered real preview output (previews open as sheet tabs). Its
      // optional-activeCell dataRequirement witness moved to the evidence view.
      // projection_status retired from the dock to a status-bar chip; no
      // longer a hideable dock tab.
      // watches removed with the Watches dock tab.
      // plugins retired from the dock entirely — not a visibility target
      // because it is no longer a dock contribution at all.
    ],
  },
  {
    host: 'leftSidebar',
    mode: 'panel',
    locationId: 'leftSidebar',
    locationLabel: 'Left Sidebar',
    menuSectionTitle: 'Sidebar Panels',
    nonHideableContributionIds: [],
    // Search + Copilot retired from the sidebar (they became the ⌘K palette
    // SEARCH section and the ✧ Focus popover), so they are no longer
    // hideable left-sidebar targets. The remaining panels re-home into Discover.
    firstPartyVisibilityTargetIds: [
      'frisket.core.panel.notifications',
      'frisket.core.panel.sources',
      'frisket.investigative.panel.friendly_filters',
      'frisket.investigative.panel.mentions',
      'frisket.core.panel.saved_views',
      'frisket.core.panel.watches',
      'frisket.embeddings.panel.indexes',
    ],
  },
];

export function workbenchNonHideableContributionIds(): Set<string> {
  const ids = new Set<string>();
  for (const policy of WORKBENCH_VISIBILITY_HOST_POLICIES) {
    for (const contributionId of policy.nonHideableContributionIds) {
      ids.add(contributionId);
    }
  }
  return ids;
}

function workbenchVisibilityPolicyForLocation(
  host: string,
  mode: string,
): WorkbenchVisibilityHostPolicy | null {
  return (
    WORKBENCH_VISIBILITY_HOST_POLICIES.find(
      (policy) => policy.host === host && policy.mode === mode,
    ) ?? null
  );
}

export type WorkbenchVisibilityTarget = {
  contributionId: string;
  title: string;
  icon?: string;
  host: WorkbenchResolvedLayoutContribution['host'];
  mode: WorkbenchResolvedLayoutContribution['mode'];
  placementId: string;
  locationId: string;
  locationLabel: string;
  locationOptions: WorkbenchVisibilityLocationOption[];
  status: WorkbenchResolvedLayoutContribution['status'];
  reason?: string;
  hideable: boolean;
  runtimeSource: WorkbenchResolvedLayoutContribution['runtimeSource'];
};

export type WorkbenchVisibilityLocationOption = {
  id: string;
  label: string;
  host: WorkbenchResolvedLayoutContribution['host'];
  mode: WorkbenchResolvedLayoutContribution['mode'];
};

function locationOptionForPolicy(
  policy: WorkbenchVisibilityHostPolicy,
): WorkbenchVisibilityLocationOption {
  return {
    id: policy.locationId,
    label: policy.locationLabel,
    host: policy.host,
    mode: policy.mode,
  };
}

export function contributionSlug(contributionId: string): string {
  return contributionId.replace(/[^a-zA-Z0-9]+/g, '-');
}

export function workbenchVisibilityTargetFromContribution(
  contribution: WorkbenchResolvedLayoutContribution,
): WorkbenchVisibilityTarget | null {
  const policy = workbenchVisibilityPolicyForLocation(contribution.host, contribution.mode);
  if (!policy) return null;
  if (policy.nonHideableContributionIds.includes(contribution.contributionId)) return null;
  if (
    contribution.runtimeSource !== 'runtimeIndex' &&
    !policy.firstPartyVisibilityTargetIds.includes(contribution.contributionId)
  ) {
    return null;
  }
  const location = locationOptionForPolicy(policy);
  return {
    contributionId: contribution.contributionId,
    title: contribution.title,
    host: contribution.host,
    mode: contribution.mode,
    placementId: contribution.placementId,
    locationId: location.id,
    locationLabel: location.label,
    locationOptions: [location],
    status: contribution.status,
    reason: contribution.reason,
    hideable: true,
    runtimeSource: contribution.runtimeSource,
  };
}

export function isSupportedWorkbenchVisibilityTarget(
  target: WorkbenchVisibilityTarget,
): boolean {
  return Boolean(
    target.hideable &&
      workbenchVisibilityPolicyForLocation(target.host, target.mode) &&
      target.locationOptions.some(
        (location) =>
          location.id === target.locationId &&
          location.host === target.host &&
          location.mode === target.mode,
      ),
  );
}

// Hide/reveal is contribution-scoped, so surfaces that act on a contribution
// (rail recovery, palette hide/reveal commands) show one entry per
// contribution even when it has targets in several visibility-managed hosts.
export function uniqueVisibilityTargetsByContribution(
  targets: WorkbenchVisibilityTarget[],
): WorkbenchVisibilityTarget[] {
  const seen = new Set<string>();
  return targets.filter((target) => {
    if (seen.has(target.contributionId)) return false;
    seen.add(target.contributionId);
    return true;
  });
}
