import '../workbench/contributions.css';
import { memo, useEffect, useMemo, useRef } from 'react';
import { AtSign, Bell, BellRing, BookOpen, Boxes, Database, Images, ListFilter, MapPin, Network, Puzzle, Tags, Watch, type LucideIcon } from 'lucide-react';
import { canEditProject } from '../api/projectRole';
import { DiscoverPanel, type DiscoverTabDescriptor } from '../workbench/DiscoverPanel';
import { DISCOVER_TABS, type DiscoverTab } from './useWorkspaceChromeState';
import { EmbeddingsWorkbenchPanel, FriendlyFiltersWorkbenchPanel, MentionsWorkbenchPanel, NotificationsWorkbenchPanel, ResolvedWorkbenchLayoutRegion, SourcesWorkbenchPanel, SavedViewsWorkbenchPanel, WatchesWorkbenchPanel } from '../workbench/contributions';
import { type WorkbenchResolvedLayoutContribution, type WorkbenchResolvedLayoutRegion } from '../workbench/layout';
import { PluginPanelHost } from '../workbench/PluginPanelHost';
import { EMBEDDINGS_CONTRIBUTION_ID, FRIENDLY_FILTERS_CONTRIBUTION_ID, MENTIONS_CONTRIBUTION_ID, NOTIFICATIONS_CONTRIBUTION_ID, SAVED_VIEWS_CONTRIBUTION_ID, SOURCES_CONTRIBUTION_ID, WATCHES_CONTRIBUTION_ID } from './contributionIds';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useChromeHandle } from '../bind/useChromeHandle';
import { useDetailHandle } from '../bind/useDetailHandle';
import { useSelector } from '../bind/useSelector';
import { useWorkspaceShell, useWorkspacePluginDetailRender, useWorkbenchHostContext, useResolvedWorkbenchLayout, useCurrentSheet, useMainViewModel, resolveRegionContributions, useRenderCount } from './WorkspaceContext';

function SidebarContributionBody({ contribution }: { contribution: WorkbenchResolvedLayoutContribution }) {
  const { projectApi } = useWorkspaceStores();
  const {
    applyLens,
    leftSidebarPluginPanelDescriptors,
    openSourceHealthMainView,
    project,
    refreshHistory,
    refreshSheets,
    selectSheet,
    invalidateProjectData,
  } = useWorkspaceShell();
  const renderPluginDetailTab = useWorkspacePluginDetailRender();
  const workbenchHostContext = useWorkbenchHostContext();
  const resolvedWorkbenchLayout = useResolvedWorkbenchLayout();
  const { sheet, sheets } = useCurrentSheet();
  const {
    applySavedView,
    canCreateSavedView,
    cancelSavedViewEditor,
    cancelSavedViewsConfirmation,
    consumeSavedViewsPublication,
    deleteView,
    replaceSavedViewDefinition,
    renameSavedView,
    saveCurrentView,
    savedViewsConfirmation,
    savedViewsConfirmationError,
    savedViewsEditor,
    savedViewsLastPublication,
    savedViewsSaving,
    activeSavedViewId,
    addWatchForCurrentView,
    openCellSimilarity,
    selectedRowIdsForSheet,
    setViewName,
    startCreatingSavedView,
    startEditingView,
    startSavedViewDefinitionUpdate,
    startSavedViewDelete,
    viewName,
    views,
  } = useMainViewModel();

  switch (contribution.contributionId) {
    case 'frisket.core.panel.notifications':
      return <NotificationsWorkbenchPanel projectId={project.id} />;
    case 'frisket.core.panel.sources':
      return (
        <SourcesWorkbenchPanel
          sourceApi={projectApi}
          sourceDetailContributions={
            resolveRegionContributions(resolvedWorkbenchLayout, 'sourceDetail').contributions
          }
          renderPluginDetailTab={renderPluginDetailTab}
          onPolled={(sheetId) => {

            void refreshSheets().then(() => {
              if (sheetId != null) selectSheet(String(sheetId));
            });
            void refreshHistory();
            invalidateProjectData();
          }}
          onOpenSourceHealthMainView={openSourceHealthMainView}
        />
      );
    case 'frisket.investigative.panel.friendly_filters':
      return (
        <FriendlyFiltersWorkbenchPanel
          sheets={sheets}
          hostContext={workbenchHostContext}
        />
      );
    case 'frisket.investigative.panel.mentions':
      return (
        <MentionsWorkbenchPanel
          sheets={sheets}
          hostContext={workbenchHostContext}
        />
      );
    case SAVED_VIEWS_CONTRIBUTION_ID:
      return (
        <SavedViewsWorkbenchPanel
          views={views}
          viewName={viewName}
          editor={savedViewsEditor}
          activeSavedViewId={activeSavedViewId}
          isSaving={savedViewsSaving}
          lastPublication={savedViewsLastPublication}
          onPublicationConsumed={consumeSavedViewsPublication}
          confirmation={savedViewsConfirmation}
          confirmationError={savedViewsConfirmationError}
          canEdit={canCreateSavedView}
          onNameChange={setViewName}
          onStartCreating={() => startCreatingSavedView()}
          onSave={saveCurrentView}
          onUpdate={renameSavedView}
          onCancel={cancelSavedViewEditor}
          onEdit={startEditingView}
          onApply={applySavedView}
          onStartDefinitionUpdate={startSavedViewDefinitionUpdate}
          onStartDelete={startSavedViewDelete}
          onConfirmDefinitionUpdate={replaceSavedViewDefinition}
          onConfirmDelete={deleteView}
          onCancelConfirmation={cancelSavedViewsConfirmation}
        />
      );
    case 'frisket.core.panel.watches':
      return (
        <WatchesWorkbenchPanel
          canEdit={canEditProject(project)}
          canCreateFromCurrentView={sheet !== null && canEditProject(project)}
          onCreateFromCurrentView={addWatchForCurrentView}
        />
      );
    case 'frisket.embeddings.panel.indexes':
      return sheet ? (
        <EmbeddingsWorkbenchPanel
          apiPort={projectApi}
          projectId={project.id}
          key={sheet.id}
          sheet={sheet}
          onSelectSheet={(sheetId) => {

            void refreshSheets().then(() => selectSheet(sheetId));
          }}
          onOpenLens={(lensId, name) => {
            void applyLens(lensId, name);
          }}
          selectedRowIds={selectedRowIdsForSheet.map(String)}
          onFindSimilar={openCellSimilarity}
        />
      ) : null;
    default: {
      if (contribution.runtimeSource === 'runtimeIndex') {
        const descriptor = leftSidebarPluginPanelDescriptors.find(
          (candidate) => candidate.id === contribution.contributionId,
        );
        if (descriptor) {
          return (
            <PluginPanelHost
              descriptor={descriptor}
              sheet={sheet ?? null}
              hostContext={workbenchHostContext}
              host="leftSidebar"
            />
          );
        }
      }
      return (
        <div
          className="sidebar-contribution-unavailable"
          data-testid="sidebar-unavailable-contribution"
          data-contribution-id={contribution.contributionId}
        >
          This panel&apos;s content is unavailable.
        </div>
      );
    }
  }
}

const NON_DISCOVER_LEFT_SIDEBAR_CONTRIBUTION_IDS = new Set<string>();

const DISCOVER_TAB_CONTRIBUTION_IDS: Record<DiscoverTab, readonly string[]> = {
  Facets: [FRIENDLY_FILTERS_CONTRIBUTION_ID],
  Mentions: [MENTIONS_CONTRIBUTION_ID],
  Sources: [SOURCES_CONTRIBUTION_ID],
  Views: [SAVED_VIEWS_CONTRIBUTION_ID],
  Watches: [WATCHES_CONTRIBUTION_ID],
  Embeddings: [EMBEDDINGS_CONTRIBUTION_ID],
  Notifications: [NOTIFICATIONS_CONTRIBUTION_ID],
};

const DISCOVER_TAB_ICON: Record<DiscoverTab, LucideIcon> = {
  Facets: Tags,
  Mentions: AtSign,
  Sources: Database,
  Views: BookOpen,
  Watches: Watch,
  Embeddings: Boxes,
  Notifications: BellRing,
};

const PLUGIN_TAB_ICONS: Record<string, LucideIcon> = {
  AtSign,
  Bell,
  Boxes,
  Database,
  Images,
  ListFilter,
  MapPin,
  Network,
  Tags,
};

function pluginTabIcon(iconName?: string): LucideIcon {
  return (iconName && PLUGIN_TAB_ICONS[iconName]) || Puzzle;
}

function contributionTestSlug(contributionId: string): string {
  return contributionId.replace(/[^a-zA-Z0-9]+/g, '-');
}

function filterRegionContributions(
  region: WorkbenchResolvedLayoutRegion,
  keep: (contributionId: string) => boolean,
): WorkbenchResolvedLayoutRegion {
  return {
    ...region,
    contributions: region.contributions.filter((c) => keep(c.contributionId)),
  };
}

function DiscoverTabBody({ tabId }: { tabId: string }) {
  const resolvedWorkbenchLayout = useResolvedWorkbenchLayout();
  const leftSidebarRegion = resolveRegionContributions(resolvedWorkbenchLayout, 'leftSidebar');
  const contributionById = useMemo(() => {
    const map = new Map<string, WorkbenchResolvedLayoutContribution>();
    for (const c of leftSidebarRegion.contributions) map.set(c.contributionId, c);
    return map;
  }, [leftSidebarRegion]);
  const firstPartyIds = DISCOVER_TAB_CONTRIBUTION_IDS[tabId as DiscoverTab];
  const contributionIds = firstPartyIds ?? [tabId];
  return (
    <>
      {contributionIds.map((contributionId) => {
        const contribution = contributionById.get(contributionId);
        if (!contribution || (contribution.status !== 'enabled' && contribution.status !== 'disabled')) {
          return null;
        }
        return (
          <div
            key={contributionId}
            className="discover-contribution"
            data-testid={`discover-contribution-${contributionTestSlug(contributionId)}`}
            data-contribution-id={contributionId}
          >
            <SidebarContributionBody contribution={contribution} />
          </div>
        );
      })}
    </>
  );
}

export const WorkspaceDiscoverRegion = memo(function WorkspaceDiscoverRegion() {
  useRenderCount('discover');
  const { project, setDiscoverTab, setDiscoverOpen, openDiscover, actionPanelVisible, closeActionRoute } =
    useWorkspaceShell();
  const resolvedWorkbenchLayout = useResolvedWorkbenchLayout();
  const chrome = useChromeHandle();
  const discoverOpen = useSelector(chrome.store, (s) => s.discoverOpen);
  const discoverTab = useSelector(chrome.store, (s) => s.discoverTab);
  const detailHandle = useDetailHandle();

  const discoverRevealKey = `${discoverOpen ? '1' : '0'}:${discoverTab}`;
  const previousDiscoverRevealKey = useRef(discoverRevealKey);
  useEffect(() => {
    if (previousDiscoverRevealKey.current === discoverRevealKey) return;
    previousDiscoverRevealKey.current = discoverRevealKey;
    if (actionPanelVisible) {
      chrome.hideActionPanel();
      detailHandle.setProposalInspect(null);
      closeActionRoute();
    }
  }, [discoverRevealKey, actionPanelVisible, chrome, detailHandle, closeActionRoute]);

  const leftSidebarRegion = resolveRegionContributions(resolvedWorkbenchLayout, 'leftSidebar');

  const pluginDiscoverContributions = useMemo(
    () =>
      leftSidebarRegion.contributions.filter(
        (c) =>
          c.runtimeSource === 'runtimeIndex' &&
          (c.status === 'enabled' || c.status === 'disabled'),
      ),
    [leftSidebarRegion],
  );

  const tabs = useMemo<DiscoverTabDescriptor[]>(() => {
    const firstParty = DISCOVER_TABS.map((tab) => ({
      id: tab,
      label: tab === 'Facets' ? 'Filter' : tab,
      slug: tab,
      Icon: DISCOVER_TAB_ICON[tab],
    }));
    const plugin = pluginDiscoverContributions.map((c) => ({
      id: c.contributionId,
      label: c.shortTitle ?? c.title,
      slug: contributionTestSlug(c.contributionId),
      Icon: pluginTabIcon(c.icon),
    }));
    return [...firstParty, ...plugin];
  }, [pluginDiscoverContributions]);

  const activeTab = tabs.some((tab) => tab.id === discoverTab)
    ? discoverTab
    : tabs[0]?.id ?? 'Facets';

  const discoverRegion = filterRegionContributions(
    leftSidebarRegion,
    (id) => !NON_DISCOVER_LEFT_SIDEBAR_CONTRIBUTION_IDS.has(id),
  );

  return (
    <section
      className="workbench-region workbench-discover"
      data-testid="workbench-region-discover"
      aria-label="Discover"
      data-discover-open={discoverOpen ? 'true' : 'false'}
    >
      <ResolvedWorkbenchLayoutRegion region={discoverRegion}>
        <DiscoverPanel
          projectId={project.id}
          open={discoverOpen}
          tabs={tabs}
          activeTab={activeTab}
          onSelectTab={setDiscoverTab}
          onCollapse={() => setDiscoverOpen(false)}
          onExpand={() => setDiscoverOpen(true)}
          onOpenTab={openDiscover}
          TabBody={DiscoverTabBody}
        />
      </ResolvedWorkbenchLayoutRegion>
    </section>
  );
});
