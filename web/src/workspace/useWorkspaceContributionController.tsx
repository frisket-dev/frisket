import { useCallback, useMemo } from 'react';
import type { SheetMeta, ActionTemplate } from '../api/open';
import type { CommandContext } from '../core/commands/types';
import { usePluginLayoutHandle } from '../bind/usePluginLayoutHandle';
import { useSelector } from '../bind/useSelector';
import { resolveRibbonTabs, type RibbonPluginLauncher } from '../workbench/actSurface';
import {
  dataRequirementReason, firstMissingDataRequirement, type WorkbenchDataRequirementContext,
} from '../workbench/dataRequirements';
import {
  FIRST_PARTY_WORKBENCH_CONTRIBUTION_DESCRIPTORS, IMAGE_GALLERY_VIEW_DESCRIPTOR,
  isProjectionViewDescriptor, OCR_COMPARE_COMMAND_DESCRIPTOR, TRANSCRIBE_COMPARE_COMMAND_DESCRIPTOR,
  TRANSLATE_COMPARE_COMMAND_DESCRIPTOR, TOPIC_COMPARE_COMMAND_DESCRIPTOR,
  OPEN_SOURCES_COMMAND_DESCRIPTOR, OPEN_SETTINGS_COMMAND_DESCRIPTOR,
  normalizePlacement, type WorkbenchHostId, type WorkbenchViewDescriptor,
} from '../workbench/descriptors';
import type { WorkbenchHostContext } from '../workbench/hostContext';
import {
  resolveWorkbenchLayout, type WorkbenchResolvedLayoutContribution,
  type WorkbenchStampedContributionDescriptor,
} from '../workbench/layout';
import { PluginDetailHost } from '../workbench/PluginDetailHost';
import type { PluginDetailSubject } from '../workbench/pluginDetailContext';
import { resolvePluginProjectionViewAvailability } from '../workbench/pluginProjectionViewContext';
import { resolvePluginViewAvailability } from '../workbench/pluginViewContext';
import {
  pluginCommandDescriptorsFromRuntimeIndex, pluginPanelDescriptorsFromRuntimeIndex,
  pluginViewDescriptorsFromRuntimeIndex,
} from '../workbench/pluginRuntimeDescriptors';
import {
  firstPartyCommandEntry, pluginCommandEntry, type WorkbenchCommandEntry,
} from '../workbench/commandRegistry';
import {
  workbenchVisibilityTargetFromContribution, type WorkbenchVisibilityTarget,
} from '../workbench/visibility';
import { EMPTY_MISSING_CONTRIBUTION_IDS } from './contributionIds';
import { usePluginPeekController } from './usePluginPeekController';
import type { useWorkspaceChromeState } from './useWorkspaceChromeState';
import type { useWorkspaceViewController } from './useWorkspaceViewController';
import type { useWorkbenchContributionVisibility } from './useWorkbenchContributionVisibility';

function stampRuntimeIndex<T extends WorkbenchStampedContributionDescriptor['descriptor']>(
  descriptors: T[],
): WorkbenchStampedContributionDescriptor[] {
  return descriptors.map((descriptor) => ({ descriptor, runtimeSource: 'runtimeIndex' }));
}

/** Resolves each plugin surface from the runtime index, current availability, and trusted command host. */
export function useWorkspaceContributionController({
  sheet, actActionTemplates, commandContext, workbenchHostContext,
  workbenchDataRequirementContext, chromeState, visibility, view, mapContributionId,
}: {
  sheet: SheetMeta | undefined;
  actActionTemplates: ActionTemplate[];
  commandContext: CommandContext;
  workbenchHostContext: WorkbenchHostContext;
  workbenchDataRequirementContext: WorkbenchDataRequirementContext;
  chromeState: Pick<ReturnType<typeof useWorkspaceChromeState>,
    'recordCommandAction' | 'openDiscover' | 'setActiveBottomDockTab'>;
  visibility: Pick<ReturnType<typeof useWorkbenchContributionVisibility>,
    'hiddenContributionIds' | 'isContributionHidden' | 'revealContribution'>;
  view: Pick<ReturnType<typeof useWorkspaceViewController>,
    'mapAvailabilityStatus' | 'mapAvailabilityReason' | 'mapHidden' | 'imageGalleryAvailability'>;
  mapContributionId: string | null;
}) {
  const pluginLayout = usePluginLayoutHandle();
  const workbenchPluginRuntimeIndex = useSelector(pluginLayout.store, (state) => state.workbenchPluginRuntimeIndex);
  const { recordCommandAction, openDiscover, setActiveBottomDockTab } = chromeState;
  const { hiddenContributionIds, isContributionHidden, revealContribution } = visibility;
  const { mapAvailabilityStatus, mapAvailabilityReason, mapHidden, imageGalleryAvailability } = view;
  const activityRailPluginDescriptors = useMemo(
    () =>
      [
        ...pluginPanelDescriptorsFromRuntimeIndex(workbenchPluginRuntimeIndex),
        ...pluginViewDescriptorsFromRuntimeIndex(workbenchPluginRuntimeIndex),
      ].filter((descriptor) =>
        descriptor.placements.some((placement) => placement.host === 'activityRail'),
      ),
    [workbenchPluginRuntimeIndex],
  );
  const pluginLauncherSpecs = useMemo<RibbonPluginLauncher[]>(
    () =>
      activityRailPluginDescriptors.map((descriptor) => ({
        contributionId: descriptor.id,
        label: descriptor.shortTitle ?? descriptor.title,
        iconName: descriptor.icon,
      })),
    [activityRailPluginDescriptors],
  );

  const actRibbonTabs = useMemo(
    () => resolveRibbonTabs(actActionTemplates, workbenchDataRequirementContext, pluginLauncherSpecs),
    [actActionTemplates, workbenchDataRequirementContext, pluginLauncherSpecs],
  );

  const mainViewPluginViewDescriptors = useMemo(
    () =>
      pluginViewDescriptorsFromRuntimeIndex(
        workbenchPluginRuntimeIndex,
      ).filter((descriptor) =>
        descriptor.placements.some((placement) => placement.host === 'mainView'),
      ),
    [workbenchPluginRuntimeIndex],
  );
  const mainViewPluginViewAvailability = useMemo(
    () =>
      mainViewPluginViewDescriptors.map((descriptor) => ({
        descriptor,
        availability: isProjectionViewDescriptor(descriptor)
          ? resolvePluginProjectionViewAvailability(descriptor, sheet ?? null)
          : resolvePluginViewAvailability(descriptor, sheet ?? null),
      })),
    [mainViewPluginViewDescriptors, sheet],
  );
  const activeMainViewPluginViewDescriptor = useMemo<WorkbenchViewDescriptor | null>(
    () =>
      mainViewPluginViewAvailability.find(
        ({ descriptor, availability }) =>

          descriptor.id !== mapContributionId &&
          !isContributionHidden(descriptor.id) &&
          (availability.available ||
            availability.reason?.startsWith('missing_capability:') === true),
      )?.descriptor ?? null,
    [isContributionHidden, mainViewPluginViewAvailability, mapContributionId],
  );
  const pluginPanelDescriptors = useMemo(
    () => pluginPanelDescriptorsFromRuntimeIndex(workbenchPluginRuntimeIndex),
    [workbenchPluginRuntimeIndex],
  );
  const pluginCommandDescriptors = useMemo(
    () => pluginCommandDescriptorsFromRuntimeIndex(workbenchPluginRuntimeIndex),
    [workbenchPluginRuntimeIndex],
  );

  // Resolve plugins only through the trusted-module path.
  const modalOrPeekPluginPanelDescriptors = useMemo(
    () =>
      pluginPanelDescriptors.filter((descriptor) =>
        descriptor.placements.some(
          (placement) => placement.host === 'modalOrPeek' && placement.mode === 'peek',
        ),
      ),
    [pluginPanelDescriptors],
  );

  const {
    dismissPluginPeek,
    openPluginPeekDescriptor,
    openPluginPeekForPlugin,
    pluginPeekState,
  } = usePluginPeekController(modalOrPeekPluginPanelDescriptors);

  const workbenchCommandEntries = useMemo<WorkbenchCommandEntry[]>(() => {
    const entries: WorkbenchCommandEntry[] = [
      firstPartyCommandEntry(
        { type: 'sources.open' },
        commandContext,
        OPEN_SOURCES_COMMAND_DESCRIPTOR,
      ),
      firstPartyCommandEntry(
        { type: 'settings.open' },
        commandContext,
        OPEN_SETTINGS_COMMAND_DESCRIPTOR,
      ),
      firstPartyCommandEntry(
        { type: 'ocrCompare.open' },
        commandContext,
        OCR_COMPARE_COMMAND_DESCRIPTOR,
      ),
      firstPartyCommandEntry(
        { type: 'transcribeCompare.open' },
        commandContext,
        TRANSCRIBE_COMPARE_COMMAND_DESCRIPTOR,
      ),
      firstPartyCommandEntry(
        { type: 'translateCompare.open' },
        commandContext,
        TRANSLATE_COMPARE_COMMAND_DESCRIPTOR,
      ),
      firstPartyCommandEntry(
        { type: 'topicCompare.open' },
        commandContext,
        TOPIC_COMPARE_COMMAND_DESCRIPTOR,
      ),
      ...pluginCommandDescriptors.map((descriptor) =>
        pluginCommandEntry({
          descriptor,
          sheet: sheet ?? null,
          hostContext: workbenchHostContext,
          onRan: (ranDescriptor) => recordCommandAction(`Ran ${ranDescriptor.title}`),
          openPeek: openPluginPeekForPlugin(descriptor.ownerPluginId),
        }),
      ),
    ];
    return entries.sort((left, right) => {
      const leftOrder = left.descriptor.placements[0]?.order ?? 0;
      const rightOrder = right.descriptor.placements[0]?.order ?? 0;
      return leftOrder - rightOrder || left.descriptor.id.localeCompare(right.descriptor.id);
    });
  }, [
    commandContext,
    openPluginPeekForPlugin,
    pluginCommandDescriptors,
    recordCommandAction,
    sheet,
    workbenchHostContext,
  ]);
  const pluginDescriptorsByRegion = useMemo(() => {
    const panelsForHost = (host: WorkbenchHostId) =>
      pluginPanelDescriptors.filter((descriptor) =>
        descriptor.placements.some((placement) => placement.host === host),
      );
    const columnPanels = pluginPanelDescriptors.filter((descriptor) =>
      descriptor.placements.some((placement) =>
        placement.host === 'columnDetail' || placement.host === 'columnInspector',
      ),
    );
    return {
      mainView: mainViewPluginViewDescriptors,
      rightInspector: panelsForHost('rightInspector'),
      bottomDock: panelsForHost('bottomDock'),
      leftSidebar: panelsForHost('leftSidebar'),
      rowDetail: panelsForHost('rowDetail'),
      columnDetail: columnPanels,
      columnInspector: columnPanels,
      entityDetail: panelsForHost('entityDetail'),
      sourceDetail: panelsForHost('sourceDetail'),
      modalOrPeek: modalOrPeekPluginPanelDescriptors,
      activityRail: activityRailPluginDescriptors,
    };
  }, [
    pluginPanelDescriptors, mainViewPluginViewDescriptors,
    modalOrPeekPluginPanelDescriptors, activityRailPluginDescriptors,
  ]);
  const {
    rightInspector: rightInspectorPluginPanelDescriptors,
    bottomDock: bottomDockPluginPanelDescriptors,
    leftSidebar: leftSidebarPluginPanelDescriptors,
  } = pluginDescriptorsByRegion;

  const renderPluginDetailTab = useCallback(
    (
      contribution: WorkbenchResolvedLayoutContribution,
      subject: PluginDetailSubject,
    ) => {
      if (contribution.runtimeSource !== 'runtimeIndex' || !sheet) return null;
      const descriptor = pluginPanelDescriptors.find(
        (candidate) => candidate.id === contribution.contributionId,
      );
      if (!descriptor) return null;
      return (
        <PluginDetailHost
          descriptor={descriptor}
          sheet={sheet}
          hostContext={workbenchHostContext}
          host={contribution.host}
          subject={subject}
        />
      );
    },
    [pluginPanelDescriptors, sheet, workbenchHostContext],
  );

  const disabledContributionReasons = useMemo<Record<string, string>>(() => {
    const reasons: Record<string, string> = {};
    if (mapContributionId && mapAvailabilityStatus === 'disabled' && !mapHidden) {
      reasons[mapContributionId] = mapAvailabilityReason;
    }
    if (
      !imageGalleryAvailability.available &&
      imageGalleryAvailability.reason.startsWith('missing_capability:')
    ) {
      reasons[IMAGE_GALLERY_VIEW_DESCRIPTOR.id] = imageGalleryAvailability.reason;
    }
    for (const { descriptor, availability } of mainViewPluginViewAvailability) {

      if (descriptor.id in reasons) continue;
      if (!availability.available) {
        reasons[descriptor.id] = availability.reason;
      }
    }
    for (const descriptor of FIRST_PARTY_WORKBENCH_CONTRIBUTION_DESCRIPTORS) {
      if (descriptor.id in reasons) continue;
      const unmet = firstMissingDataRequirement(
        descriptor.dataRequirements,
        workbenchDataRequirementContext,
      );
      if (unmet) {
        reasons[descriptor.id] = dataRequirementReason(unmet);
      }
    }
    return reasons;
  }, [
    mapContributionId,
    mapAvailabilityStatus,
    mapHidden,
    mapAvailabilityReason,
    imageGalleryAvailability,
    mainViewPluginViewAvailability,
    workbenchDataRequirementContext,
  ]);

  const firstPartyContributionDescriptors = useMemo<WorkbenchStampedContributionDescriptor[]>(
    () =>
      FIRST_PARTY_WORKBENCH_CONTRIBUTION_DESCRIPTORS.map((descriptor) => ({
        descriptor,
        runtimeSource: 'firstParty',
      })),
    [],
  );

  const resolvedWorkbenchLayout = useMemo(() => resolveWorkbenchLayout({
    hiddenContributionIds,
    missingContributionIds: EMPTY_MISSING_CONTRIBUTION_IDS,
    disabledContributionReasons,
    runtimeIndexContributionIdsByRegion: Object.fromEntries(
      Object.entries(pluginDescriptorsByRegion).map(([region, descriptors]) => [
        region, descriptors.map((descriptor) => descriptor.id),
      ]),
    ),
    contributionDescriptorsByRegion: {
      ...Object.fromEntries(
        Object.entries(pluginDescriptorsByRegion).map(([region, descriptors]) => [
          region, [...firstPartyContributionDescriptors, ...stampRuntimeIndex(descriptors)],
        ]),
      ),
      rowInspector: firstPartyContributionDescriptors,
      commandPalette: firstPartyContributionDescriptors,
    },
  }), [
    hiddenContributionIds, disabledContributionReasons,
    firstPartyContributionDescriptors, pluginDescriptorsByRegion,
  ]);

  const resolvedRegion = useCallback(
    (regionId: WorkbenchHostId) => {
      const region = resolvedWorkbenchLayout.find((item) => item.regionId === regionId);
      if (!region) {
        return { regionId, contributions: [] };
      }
      return region;
    },
    [resolvedWorkbenchLayout],
  );
  const workbenchVisibilityTargets = useMemo(() => {
    const targets: WorkbenchVisibilityTarget[] = [];
    for (const region of resolvedWorkbenchLayout) {
      for (const contribution of region.contributions) {
        const target = workbenchVisibilityTargetFromContribution(contribution);
        if (target !== null) targets.push(target);
      }
    }
    return targets;
  }, [resolvedWorkbenchLayout]);

  const revealPluginLauncher = useCallback(
    (contributionId: string) => {
      const descriptor = activityRailPluginDescriptors.find(
        (candidate) => candidate.id === contributionId,
      );
      if (!descriptor) return;
      const primary = descriptor.placements.find(
        (placement) => placement.host !== 'activityRail',
      );
      if (!primary) return;
      // Unhide a target before focusing it.

      if (isContributionHidden(contributionId)) {
        revealContribution(contributionId);
      }
      switch (primary.host) {
        case 'bottomDock':

          setActiveBottomDockTab(normalizePlacement(descriptor, primary).placementId);
          return;
        case 'leftSidebar':

          openDiscover(contributionId);
          return;
        case 'mainView':
        default:

          return;
      }
    },
    [
      activityRailPluginDescriptors,
      isContributionHidden,
      openDiscover,
      revealContribution,
      setActiveBottomDockTab,
    ],
  );

  const pluginLauncherCommands = useMemo(
    () =>
      activityRailPluginDescriptors.map((descriptor) => ({
        contributionId: descriptor.id,
        label: descriptor.shortTitle ?? descriptor.title,
      })),
    [activityRailPluginDescriptors],
  );

  const mainView = useMemo(() => ({
    resolvedRegion,
    activeMainViewPluginViewDescriptor,
    renderPluginDetailTab,
  }), [
    resolvedRegion, activeMainViewPluginViewDescriptor, renderPluginDetailTab,
  ]);
  const overlays = useMemo(() => ({
    dismissPluginPeek,
    openPluginPeekDescriptor,
    pluginPeekState,
    pluginLauncherCommands,
    renderPluginDetailTab,
    resolvedRegion,
    revealPluginLauncher,
    workbenchCommandEntries,
    workbenchVisibilityTargets,
  }), [
    dismissPluginPeek, openPluginPeekDescriptor, pluginPeekState, pluginLauncherCommands,
    renderPluginDetailTab, resolvedRegion, revealPluginLauncher, workbenchCommandEntries,
    workbenchVisibilityTargets,
  ]);

  return useMemo(() => ({
    actRibbonTabs, rightInspectorPluginPanelDescriptors, bottomDockPluginPanelDescriptors,
    leftSidebarPluginPanelDescriptors, renderPluginDetailTab, resolvedWorkbenchLayout,
    revealPluginLauncher, mainView, overlays,
  }), [
    actRibbonTabs, rightInspectorPluginPanelDescriptors, bottomDockPluginPanelDescriptors,
    leftSidebarPluginPanelDescriptors, renderPluginDetailTab, resolvedWorkbenchLayout,
    revealPluginLauncher, mainView, overlays,
  ]);
}
