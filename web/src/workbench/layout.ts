import {
  WORKBENCH_SLOT_BY_HOST,
  normalizePlacement,
  type WorkbenchContributionDescriptor,
  type WorkbenchHostId,
  type WorkbenchPlacement,
  type WorkbenchPlacementMode,
  type WorkbenchPlacementSlot,
} from './descriptors';

export type WorkbenchResolvedContributionStatus =
  | 'enabled'
  | 'disabled'
  | 'hidden'
  | 'missing';

export interface WorkbenchResolvedLayoutContribution {
  contributionId: string;
  title: string;
  shortTitle?: string;
  icon?: string;
  ownerPluginId: string;
  host: WorkbenchHostId;
  mode: WorkbenchPlacementMode;
  slot: WorkbenchPlacementSlot;
  placementId: string;
  tabChrome?: WorkbenchPlacement['tabChrome'];
  status: WorkbenchResolvedContributionStatus;
  reason?: string;
  runtimeSource: 'firstParty' | 'runtimeIndex' | 'layoutProfile';
}

export interface WorkbenchResolvedLayoutRegion {
  regionId: WorkbenchHostId;
  contributions: WorkbenchResolvedLayoutContribution[];
}

// The merged-stream entry. The caller (App.tsx) stamps every descriptor
// with its trust level and lists first-party entries first — that ordering
// IS the first-party-wins collision rule below, not a separate
// first-party-vs-plugin branch.
export interface WorkbenchStampedContributionDescriptor {
  descriptor: WorkbenchContributionDescriptor;
  runtimeSource: 'firstParty' | 'runtimeIndex';
}

// Every workbench host resolves — detail hosts and the command palette get the
// same visibility/recovery/disabled-reason plumbing as the six regions.
const WORKBENCH_HOST_IDS: WorkbenchHostId[] = [
  'activityRail',
  'leftSidebar',
  'mainView',
  'rightInspector',
  'bottomDock',
  'modalOrPeek',
  'rowDetail',
  'entityDetail',
  'sourceDetail',
  'columnDetail',
  'rowInspector',
  'columnInspector',
  'commandPalette',
];

// Mode reported for runtime-index placeholders (contributions known to the
// runtime index but lacking a resolved descriptor). One entry per host — the
// mode a placement in that host would use.
export const WORKBENCH_PLACEHOLDER_MODE_BY_HOST: Record<
  WorkbenchHostId,
  WorkbenchPlacementMode
> = {
  activityRail: 'panel',
  leftSidebar: 'panel',
  mainView: 'pane',
  rightInspector: 'panel',
  bottomDock: 'tab',
  modalOrPeek: 'peek',
  rowDetail: 'tab',
  entityDetail: 'tab',
  sourceDetail: 'tab',
  columnDetail: 'tab',
  rowInspector: 'section',
  columnInspector: 'section',
  commandPalette: 'command',
};

function resolveWorkbenchLayoutRegion({
  regionId,
  hiddenContributionIds = [],
  missingContributionIds = [],
  disabledContributionReasons = {},
  runtimeIndexContributionIds = [],
  contributionDescriptors = [],
}: {
  regionId: WorkbenchHostId;
  hiddenContributionIds?: string[];
  missingContributionIds?: string[];
  disabledContributionReasons?: Record<string, string>;
  runtimeIndexContributionIds?: string[];
  contributionDescriptors?: WorkbenchStampedContributionDescriptor[];
}): WorkbenchResolvedLayoutRegion {
  const hidden = new Set(hiddenContributionIds);
  const missing = new Set(missingContributionIds);
  const disabled = new Map(Object.entries(disabledContributionReasons));
  const runtimeIndex = new Set(runtimeIndexContributionIds);
  const placementOrderById = new Map<string, number>();
  // ONE merged stream, first-party-wins ordering: the first entry to place
  // a contribution id in this region wins; a later entry with the same id
  // (e.g. a plugin claiming a first-party id) is skipped entirely.
  const seenContributionIds = new Set<string>();
  const resolvedContributions: WorkbenchResolvedLayoutContribution[] = [];
  for (const { descriptor, runtimeSource } of contributionDescriptors) {
    if (seenContributionIds.has(descriptor.id)) continue;
    let placedInRegion = false;
    for (const placement of descriptor.placements) {
      if (placement.host !== regionId) continue;
      placedInRegion = true;
      const normalized = normalizePlacement(descriptor, placement);
      const status = missing.has(descriptor.id)
        ? 'missing'
        : hidden.has(descriptor.id)
          ? 'hidden'
          : disabled.has(descriptor.id)
            ? 'disabled'
            : 'enabled';
      placementOrderById.set(`${descriptor.id}:${normalized.placementId}`, normalized.order ?? 0);
      resolvedContributions.push({
        contributionId: descriptor.id,
        title: descriptor.title,
        shortTitle: descriptor.shortTitle,
        icon: descriptor.icon,
        ownerPluginId: descriptor.ownerPluginId,
        host: regionId,
        mode: normalized.mode,
        slot: normalized.slot,
        placementId: normalized.placementId,
        tabChrome: normalized.tabChrome,
        status,
        reason:
          status === 'missing'
            ? 'missing_plugin'
            : status === 'hidden'
              ? 'hidden_by_profile'
              : status === 'disabled'
                ? disabled.get(descriptor.id)
                : undefined,
        runtimeSource,
      });
    }
    if (placedInRegion) seenContributionIds.add(descriptor.id);
  }
  resolvedContributions.sort((left, right) => {
    const leftOrder = placementOrderById.get(`${left.contributionId}:${left.placementId}`) ?? 0;
    const rightOrder = placementOrderById.get(`${right.contributionId}:${right.placementId}`) ?? 0;
    return leftOrder - rightOrder || left.contributionId.localeCompare(right.contributionId);
  });

  const resolvedContributionIds = new Set(
    resolvedContributions.map((item) => item.contributionId),
  );
  const runtimePlaceholders: WorkbenchResolvedLayoutContribution[] = [];
  for (const contributionId of runtimeIndex) {
    if (resolvedContributionIds.has(contributionId)) continue;
    runtimePlaceholders.push({
      contributionId,
      title: contributionId,
      ownerPluginId: contributionId.split('.').slice(0, 2).join('.') || 'runtime',
      host: regionId,
      mode: WORKBENCH_PLACEHOLDER_MODE_BY_HOST[regionId],
      slot: WORKBENCH_SLOT_BY_HOST[regionId],
      placementId: `${contributionId}:${regionId}:runtime-index`,
      status: 'missing',
      reason: 'runtime_index_pending_activation',
      runtimeSource: 'runtimeIndex',
    });
  }

  return {
    regionId,
    contributions: [...resolvedContributions, ...runtimePlaceholders],
  };
}

export function resolveWorkbenchLayout({
  hiddenContributionIds = [],
  missingContributionIds = [],
  disabledContributionReasons = {},
  runtimeIndexContributionIdsByRegion = {},
  contributionDescriptorsByRegion = {},
}: {
  hiddenContributionIds?: string[];
  missingContributionIds?: string[];
  disabledContributionReasons?: Record<string, string>;
  runtimeIndexContributionIdsByRegion?: Partial<Record<WorkbenchHostId, string[]>>;
  contributionDescriptorsByRegion?: Partial<
    Record<WorkbenchHostId, WorkbenchStampedContributionDescriptor[]>
  >;
} = {}): WorkbenchResolvedLayoutRegion[] {
  return WORKBENCH_HOST_IDS.map((regionId) =>
    resolveWorkbenchLayoutRegion({
      regionId,
      hiddenContributionIds,
      missingContributionIds,
      disabledContributionReasons,
      runtimeIndexContributionIds: runtimeIndexContributionIdsByRegion[regionId] ?? [],
      contributionDescriptors: contributionDescriptorsByRegion[regionId] ?? [],
    }),
  );
}
