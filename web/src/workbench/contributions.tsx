import '../components/EntityDetailPanel.css';
import './WorkbenchCommandPalette.css';
import './contributions.css';
import type { ReactNode } from 'react';
import { useActSurfaceHandle } from '../bind/useActSurfaceHandle';
import { EmbeddingsPanel, type EmbeddingsPanelProps } from '../components/EmbeddingsPanel';
import { FriendlyFiltersPanel, type FriendlyFiltersPanelProps } from '../components/FriendlyFiltersPanel';
import { HistoryPanel, type HistoryPanelProps } from '../components/HistoryPanel';
import { MentionsPanel, type MentionsPanelProps } from '../components/MentionsPanel';
import { MENTIONS_EXTRACT_ACTION_KIND } from '../components/mentionsPanelModel';
import { NotificationsPanel } from '../components/NotificationsPanel';
import { FeedsPanel, type FeedsPanelProps } from '../components/FeedsPanel';
import { AlertsPanel } from '../components/AlertsPanel';
import { ViewsPanel, type ViewsPanelProps } from '../workspace/ViewsPanel';
import {
  hostContextCapabilityAttribute,
  type WorkbenchHostContext,
} from './hostContext';
import {
  COLUMN_RUNS_SECTION_DESCRIPTOR,
  COLUMN_SETTINGS_SECTION_DESCRIPTOR,
  COST_GATE_VIEW_DESCRIPTOR,
  EMBEDDINGS_DESCRIPTOR,
  ENTITY_CONNECTIONS_VIEW_DESCRIPTOR,
  ENTITY_CITATIONS_VIEW_DESCRIPTOR,
  FRIENDLY_FILTERS_DESCRIPTOR,
  ENTITY_SUMMARY_VIEW_DESCRIPTOR,
  CITATION_VIEW_DESCRIPTOR,
  GRAPH_NEIGHBORHOOD_VIEW_DESCRIPTOR,
  GRID_VIEW_DESCRIPTOR,
  HISTORY_DESCRIPTOR,
  IMAGE_GALLERY_VIEW_DESCRIPTOR,
  MENTIONS_DESCRIPTOR,
  NOTIFICATIONS_DESCRIPTOR,
  GENERATED_COLUMN_COLLISION_VIEW_DESCRIPTOR,
  PROVENANCE_PANEL_DESCRIPTOR,
  REVIEW_QUEUE_VIEW_DESCRIPTOR,
  ROW_DELETE_CONFIRM_VIEW_DESCRIPTOR,
  ROW_INSPECTOR_CITATIONS_SECTION_DESCRIPTOR,
  VIEWS_DESCRIPTOR,
  FEEDS_DESCRIPTOR,
  FEED_HEALTH_VIEW_DESCRIPTOR,
  FEED_KIND_FORM_DESCRIPTORS,
  FEED_CHECKS_VIEW_DESCRIPTOR,
  FEED_SUMMARY_VIEW_DESCRIPTOR,
  ALERTS_DESCRIPTOR,
  actionFormDescriptor,
  normalizePlacement,
  type WorkbenchCommandDescriptor,
  type WorkbenchContributionDescriptor,
  type ColumnInspectorSectionDescriptor,
  type WorkbenchHostId,
  type WorkbenchRegionId,
  type WorkbenchRequirement,
  type WorkbenchResolvedPlacement,
} from './descriptors';
import type { WorkbenchResolvedLayoutRegion } from './layout';
import { contributionSlug } from './visibility';

function contributionIdsByStatus(
  region: WorkbenchResolvedLayoutRegion,
  status: WorkbenchResolvedLayoutRegion['contributions'][number]['status'],
): string {
  const ids: string[] = [];
  for (const item of region.contributions) {
    if (item.status === status) ids.push(item.contributionId);
  }
  return ids.join(' ');
}

export function ResolvedWorkbenchLayoutRegion({
  region,
  children,
}: {
  region: WorkbenchResolvedLayoutRegion;
  children?: ReactNode;
}) {
  return (
    <div
      className="workbench-resolved-layout-region"
      data-testid={`workbench-resolved-layout-region-${region.regionId}`}
      data-renderer="ResolvedWorkbenchLayoutRendererV1"
      data-region-id={region.regionId}
      data-contribution-ids={region.contributions.map((item) => item.contributionId).join(' ')}
      data-enabled-contribution-ids={contributionIdsByStatus(region, 'enabled')}
      data-hidden-contribution-ids={contributionIdsByStatus(region, 'hidden')}
      data-disabled-contribution-ids={contributionIdsByStatus(region, 'disabled')}
      data-missing-contribution-ids={contributionIdsByStatus(region, 'missing')}
    >
      {region.contributions.map((item) => (
        <span
          key={`${item.contributionId}:${item.placementId}`}
          hidden
          data-testid={`workbench-resolved-layout-item-${contributionSlug(item.contributionId)}`}
          data-contribution-id={item.contributionId}
          data-host={item.host}
          data-mode={item.mode}
          data-slot={item.slot}
          data-placement-id={item.placementId}
          data-status={item.status}
          data-reason={item.reason}
          data-runtime-source={item.runtimeSource}
        />
      ))}
      {children}
    </div>
  );
}

interface FriendlyFiltersWorkbenchPanelProps extends FriendlyFiltersPanelProps {
  host?: WorkbenchRegionId;
  hostContext?: WorkbenchHostContext;
}

interface MentionsWorkbenchPanelProps extends MentionsPanelProps {
  host?: WorkbenchRegionId;
  hostContext?: WorkbenchHostContext;
}


interface NotificationsWorkbenchPanelProps {
  host?: WorkbenchRegionId;
  projectId: string;
}

interface ViewsWorkbenchPanelProps extends ViewsPanelProps {
  host?: WorkbenchRegionId;
}

interface AlertsWorkbenchPanelProps {
  canEdit: boolean;
  canCreateFromCurrentView?: boolean;
  host?: WorkbenchRegionId;
  onCreateFromCurrentView?(): void;
}

interface EmbeddingsWorkbenchPanelProps extends EmbeddingsPanelProps {
  host?: WorkbenchRegionId;
}

interface FeedsWorkbenchPanelProps extends FeedsPanelProps {
  host?: WorkbenchRegionId;
}

interface HistoryWorkbenchPanelProps extends HistoryPanelProps {
  host?: WorkbenchRegionId;
}

function contributionTestId(contributionId: string): string {
  return `workbench-contribution-${contributionSlug(contributionId)}`;
}

function commandTestId(contributionId: string): string {
  return `workbench-command-${contributionSlug(contributionId)}`;
}

interface WorkbenchContributionFrameProps {
  descriptor: WorkbenchContributionDescriptor;
  host?: WorkbenchHostId;
  className?: string;
  dataAttributes?: Record<string, string | undefined>;
  children: ReactNode;
}

function placementForHost(
  descriptor: WorkbenchContributionDescriptor,
  host: WorkbenchHostId,
): WorkbenchResolvedPlacement {
  const placement =
    descriptor.placements.find((candidate) => candidate.host === host) ??
    descriptor.placements.find((candidate) => candidate.default) ??
    descriptor.placements[0];
  return normalizePlacement(descriptor, placement);
}

function requiredIds(
  descriptor: WorkbenchContributionDescriptor,
  kind: WorkbenchRequirement['kind'],
): string {
  const ids: string[] = [];
  for (const requirement of descriptor.requires) {
    if (requirement.kind !== kind) {
      continue;
    }
    if (typeof requirement.id !== 'string' || requirement.id.trim() === '') {
      throw new Error(`${descriptor.id} has an invalid ${kind} requirement id`);
    }
    ids.push(requirement.id);
  }
  return ids.join(' ');
}

function requiredCapabilities(descriptor: WorkbenchContributionDescriptor): string {
  return requiredIds(descriptor, 'hostCapability');
}

function requiredContributions(descriptor: WorkbenchContributionDescriptor): string {
  return requiredIds(descriptor, 'contribution');
}

function requiredPermissions(descriptor: WorkbenchContributionDescriptor): string {
  return requiredIds(descriptor, 'permission');
}

export function WorkbenchContributionFrame({
  descriptor,
  host = 'mainView',
  className = 'workbench-contribution-host',
  dataAttributes,
  children,
}: WorkbenchContributionFrameProps) {
  const placement = placementForHost(descriptor, host);

  return (
    <section
      className={className}
      aria-label={descriptor.title}
      data-testid={contributionTestId(descriptor.id)}
      data-schema-version={descriptor.schemaVersion}
      data-contribution-id={descriptor.id}
      data-host={placement.host}
      data-mode={placement.mode}
      data-slot={placement.slot}
      data-placement-id={placement.placementId}
      data-runtime-component-key={descriptor.componentKey}
      data-required-capabilities={requiredCapabilities(descriptor)}
      data-required-contributions={requiredContributions(descriptor)}
      data-required-permissions={requiredPermissions(descriptor)}
      {...dataAttributes}
    >
      {children}
    </section>
  );
}

export function WorkbenchCommandButtonFrame({
  descriptor,
  onClick,
  disabled = false,
  availabilityStatus,
  availabilityReason,
  active = false,
  onMouseEnter,
}: {
  descriptor: WorkbenchCommandDescriptor;
  onClick: () => void;
  disabled?: boolean;
  availabilityStatus?: string;
  availabilityReason?: string;
  /** Keyboard-nav highlight — the command palette's Commands section joins
   *  the same flat arrow-key traversal as BEST MATCH/ACTIONS/GO TO once it is
   *  query-filtered. */
  active?: boolean;
  onMouseEnter?: () => void;
}) {
  const placement = placementForHost(descriptor, 'commandPalette');

  return (
    <button
      type="button"
      className={`workbench-command-palette-item${active ? ' active' : ''}`}
      data-testid={commandTestId(descriptor.id)}
      data-schema-version={descriptor.schemaVersion}
      data-contribution-id={descriptor.id}
      data-command-id={descriptor.commandId}
      data-host={placement.host}
      data-mode={placement.mode}
      data-runtime-handler-key={descriptor.handlerKey}
      data-required-contributions={requiredContributions(descriptor)}
      data-required-capabilities={requiredCapabilities(descriptor)}
      data-slot={placement.slot}
      data-placement-id={placement.placementId}
      data-availability-status={availabilityStatus}
      data-availability-reason={availabilityReason}
      data-active={active ? 'true' : 'false'}
      disabled={disabled}
      aria-disabled={disabled}
      onMouseEnter={onMouseEnter}
      onClick={onClick}
    >
      {descriptor.title}
    </button>
  );
}

export function RowInspectorCitationsSectionFrame({ children }: { children: ReactNode }) {
  const rowDetailPlacement = placementForHost(ROW_INSPECTOR_CITATIONS_SECTION_DESCRIPTOR, 'rowDetail');
  return (
    <WorkbenchContributionFrame
      descriptor={ROW_INSPECTOR_CITATIONS_SECTION_DESCRIPTOR}
      host="rightInspector"
      className=""
      dataAttributes={{
        'data-tab-host': rowDetailPlacement.host,
        'data-tab-mode': rowDetailPlacement.mode,
        'data-tab-placement-id': rowDetailPlacement.placementId,
      }}
    >
      {children}
    </WorkbenchContributionFrame>
  );
}

function ColumnInspectorSectionFrame({
  descriptor,
  children,
}: {
  descriptor: ColumnInspectorSectionDescriptor;
  children: ReactNode;
}) {
  const columnDetailPlacement = placementForHost(descriptor, 'columnDetail');
  return (
    <WorkbenchContributionFrame
      descriptor={descriptor}
      host="columnInspector"
      className=""
      dataAttributes={{
        'data-tab-host': columnDetailPlacement.host,
        'data-tab-mode': columnDetailPlacement.mode,
        'data-tab-placement-id': columnDetailPlacement.placementId,
      }}
    >
      {children}
    </WorkbenchContributionFrame>
  );
}

export function ColumnInspectorSettingsSectionFrame({ children }: { children: ReactNode }) {
  return (
    <ColumnInspectorSectionFrame descriptor={COLUMN_SETTINGS_SECTION_DESCRIPTOR}>
      {children}
    </ColumnInspectorSectionFrame>
  );
}

export function ColumnInspectorRunsSectionFrame({ children }: { children: ReactNode }) {
  return (
    <ColumnInspectorSectionFrame descriptor={COLUMN_RUNS_SECTION_DESCRIPTOR}>
      {children}
    </ColumnInspectorSectionFrame>
  );
}

export function GridWorkbenchViewFrame({ children }: { children: ReactNode }) {
  return (
    <WorkbenchContributionFrame descriptor={GRID_VIEW_DESCRIPTOR}>
      {children}
    </WorkbenchContributionFrame>
  );
}

// MapWorkbenchViewFrame is gone: the map view is a runtime plugin
// contribution mounted through PluginMainViewHost, framed by the generic
// WorkbenchContributionFrame like every other plugin view.

export function ImageGalleryWorkbenchViewFrame({
  dataAttributes,
  children,
}: {
  dataAttributes?: Record<string, string | undefined>;
  children: ReactNode;
}) {
  return (
    <WorkbenchContributionFrame
      descriptor={IMAGE_GALLERY_VIEW_DESCRIPTOR}
      dataAttributes={dataAttributes}
    >
      {children}
    </WorkbenchContributionFrame>
  );
}

export function CitationWorkbenchViewFrame({
  host = 'modalOrPeek',
  children,
}: {
  host?: WorkbenchHostId;
  children: ReactNode;
}) {
  return (
    <WorkbenchContributionFrame descriptor={CITATION_VIEW_DESCRIPTOR} host={host}>
      {children}
    </WorkbenchContributionFrame>
  );
}

export function ReviewQueueWorkbenchViewFrame({
  host = 'modalOrPeek',
  children,
}: {
  host?: WorkbenchHostId;
  children: ReactNode;
}) {
  return (
    <WorkbenchContributionFrame descriptor={REVIEW_QUEUE_VIEW_DESCRIPTOR} host={host}>
      {children}
    </WorkbenchContributionFrame>
  );
}

export function ProvenanceWorkbenchPanelFrame({
  host = 'modalOrPeek',
  children,
}: {
  host?: WorkbenchHostId;
  children: ReactNode;
}) {
  return (
    <WorkbenchContributionFrame descriptor={PROVENANCE_PANEL_DESCRIPTOR} host={host}>
      {children}
    </WorkbenchContributionFrame>
  );
}

export function RowDeleteConfirmWorkbenchViewFrame({ children }: { children: ReactNode }) {
  return (
    <WorkbenchContributionFrame
      descriptor={ROW_DELETE_CONFIRM_VIEW_DESCRIPTOR}
      host="modalOrPeek"
      className=""
    >
      {children}
    </WorkbenchContributionFrame>
  );
}

export function CostGateWorkbenchViewFrame({ children }: { children: ReactNode }) {
  return (
    <WorkbenchContributionFrame
      descriptor={COST_GATE_VIEW_DESCRIPTOR}
      host="modalOrPeek"
      className=""
    >
      {children}
    </WorkbenchContributionFrame>
  );
}

export function GeneratedColumnCollisionWorkbenchViewFrame({
  children,
}: {
  children: ReactNode;
}) {
  return (
    <WorkbenchContributionFrame
      descriptor={GENERATED_COLUMN_COLLISION_VIEW_DESCRIPTOR}
      host="modalOrPeek"
      className=""
    >
      {children}
    </WorkbenchContributionFrame>
  );
}

export function GraphNeighborhoodWorkbenchViewFrame({ children }: { children: ReactNode }) {
  return (
    <WorkbenchContributionFrame descriptor={GRAPH_NEIGHBORHOOD_VIEW_DESCRIPTOR}>
      {children}
    </WorkbenchContributionFrame>
  );
}

// The ONE bottom-dock tab-body frame. Every Monitor dock tab body renders
// through this single owner — the byte-identical per-tab frames
// (Jobs/Errors/Lineage/Preview) and the projection / plugin-manager variants
// collapsed here. `.bottom-dock-tab-body` owns the ONE gutter (the
// .discover-body pattern); the host panel and the hosted content add none.
export function BottomDockTabFrame({
  descriptor,
  className,
  dataAttributes,
  children,
}: {
  descriptor: WorkbenchContributionDescriptor;
  className?: string;
  dataAttributes?: Record<string, string | undefined>;
  children: ReactNode;
}) {
  const classes = ['bottom-dock-tab-body', className].filter(Boolean).join(' ');
  return (
    <WorkbenchContributionFrame
      descriptor={descriptor}
      host="bottomDock"
      className={classes}
      dataAttributes={dataAttributes}
    >
      {children}
    </WorkbenchContributionFrame>
  );
}

export function EntityDetailSummaryTabFrame({ children }: { children: ReactNode }) {
  return (
    <WorkbenchContributionFrame
      descriptor={ENTITY_SUMMARY_VIEW_DESCRIPTOR}
      host="entityDetail"
      className="entity-detail-contribution-tab"
    >
      {children}
    </WorkbenchContributionFrame>
  );
}

export function EntityDetailCitationsTabFrame({ children }: { children: ReactNode }) {
  return (
    <WorkbenchContributionFrame
      descriptor={ENTITY_CITATIONS_VIEW_DESCRIPTOR}
      host="entityDetail"
      className="entity-detail-contribution-tab"
    >
      {children}
    </WorkbenchContributionFrame>
  );
}

export function EntityDetailConnectionsTabFrame({ children }: { children: ReactNode }) {
  return (
    <WorkbenchContributionFrame
      descriptor={ENTITY_CONNECTIONS_VIEW_DESCRIPTOR}
      host="entityDetail"
      className="entity-detail-contribution-tab"
    >
      {children}
    </WorkbenchContributionFrame>
  );
}

function FeedDetailSummaryTabFrame({ children }: { children: ReactNode }) {
  return (
    <WorkbenchContributionFrame
      descriptor={FEED_SUMMARY_VIEW_DESCRIPTOR}
      host="sourceDetail"
      className="source-detail-contribution-tab"
    >
      {children}
    </WorkbenchContributionFrame>
  );
}

function FeedDetailHealthTabFrame({ children }: { children: ReactNode }) {
  return (
    <WorkbenchContributionFrame
      descriptor={FEED_HEALTH_VIEW_DESCRIPTOR}
      host="sourceDetail"
      className="source-detail-contribution-tab"
    >
      {children}
    </WorkbenchContributionFrame>
  );
}

export function FeedHealthMainViewFrame({ children }: { children: ReactNode }) {
  return (
    <WorkbenchContributionFrame
      descriptor={FEED_HEALTH_VIEW_DESCRIPTOR}
      host="mainView"
    >
      {children}
    </WorkbenchContributionFrame>
  );
}

function FeedDetailChecksTabFrame({ children }: { children: ReactNode }) {
  return (
    <WorkbenchContributionFrame
      descriptor={FEED_CHECKS_VIEW_DESCRIPTOR}
      host="sourceDetail"
      className="source-detail-contribution-tab"
    >
      {children}
    </WorkbenchContributionFrame>
  );
}

// No ActionsWorkbenchPanelFrame: the actions panel is not a placed
// contribution. The shell expresses actions as the overlay ActionDrawer, whose
// per-launch form is wrapped by ActionWorkbenchFormFrame (below), not a
// resident rightInspector frame.

function FeedKindWorkbenchFormFrame({
  sourceKind,
  children,
}: {
  sourceKind: string;
  children: ReactNode;
}) {
  const descriptor = FEED_KIND_FORM_DESCRIPTORS[sourceKind];
  if (!descriptor) return <>{children}</>;
  return (
    <WorkbenchContributionFrame
      descriptor={descriptor}
      host="modalOrPeek"
      className="workbench-form-contribution"
      dataAttributes={{ 'data-accepts': descriptor.accepts.join(' ') }}
    >
      {children}
    </WorkbenchContributionFrame>
  );
}

export function ActionWorkbenchFormFrame({
  actionKind,
  title,
  children,
}: {
  actionKind: string;
  title?: string;
  children: ReactNode;
}) {
  const descriptor = actionFormDescriptor(actionKind, title);
  return (
    <WorkbenchContributionFrame
      descriptor={descriptor}
      host="modalOrPeek"
      className="workbench-form-contribution"
      dataAttributes={{
        'data-action-kind': descriptor.actionKind,
        'data-form-modes': descriptor.modes.join(' '),
      }}
    >
      {children}
    </WorkbenchContributionFrame>
  );
}

export function FriendlyFiltersWorkbenchPanel({
  host = 'leftSidebar',
  sheets,
  hostContext,
}: FriendlyFiltersWorkbenchPanelProps) {
  const descriptor = FRIENDLY_FILTERS_DESCRIPTOR;
  const placement = placementForHost(descriptor, host);
  const applySpec = hostContext?.gridFilter.applySpec;

  return (
    <WorkbenchContributionFrame
      descriptor={descriptor}
      host={placement.host}
      className=""
      dataAttributes={{
        'data-host-context': hostContext ? 'WorkbenchHostContextV1' : 'prop-callback-fallback',
        'data-host-context-capabilities': hostContext
          ? hostContextCapabilityAttribute(hostContext)
          : undefined,
      }}
    >
      <FriendlyFiltersPanel
        sheets={sheets}
        activeSheetId={hostContext?.identity.activeSheetId ?? null}
        gridFilter={hostContext?.gridState.filter ?? null}
        dataVersion={hostContext?.gridState.dataVersion ?? 0}
        onApplyFilterSpec={applySpec}
      />
    </WorkbenchContributionFrame>
  );
}

/** The Mentions panel's descriptor frame. Passes the host's STRUCTURED
 *  entity-filter capability (grid.filter.applyEntity) rather than the scalar
 *  applyValue, and wires the empty-state CTA to openActionRoute — which OPENS
 *  the map.ner drawer pre-targeted and never POSTs a run. The CTA is omitted
 *  entirely without an active sheet: there would be nothing to extract from. */
export function MentionsWorkbenchPanel({
  host = 'leftSidebar',
  sheets,
  hostContext,
}: MentionsWorkbenchPanelProps) {
  const descriptor = MENTIONS_DESCRIPTOR;
  const placement = placementForHost(descriptor, host);
  const activeSheetId = hostContext?.identity.activeSheetId ?? null;
  const openActionRoute = hostContext?.navigation.openActionRoute;

  return (
    <WorkbenchContributionFrame
      descriptor={descriptor}
      host={placement.host}
      className=""
      dataAttributes={{
        'data-host-context': hostContext ? 'WorkbenchHostContextV1' : 'prop-callback-fallback',
        'data-host-context-capabilities': hostContext
          ? hostContextCapabilityAttribute(hostContext)
          : undefined,
      }}
    >
      <MentionsPanel
        sheets={sheets}
        activeSheetId={activeSheetId}
        gridFilter={hostContext?.gridState.filter ?? null}
        // The host's "the rows changed" signal. `map.ner` creates its output
        // column when the run STARTS, so an open panel's first (and only)
        // preview is the empty one taken at that instant; without this input
        // it would keep reporting "0 of N rows extracted" after a finished
        // extraction. Threaded as data, not as a poll loop inside the panel.
        dataVersion={hostContext?.gridState.dataVersion ?? 0}
        onFilterEntity={hostContext?.gridFilter.applyEntity}
        onClearFilter={hostContext?.gridFilter.clear}
        onExtractEntities={
          openActionRoute && activeSheetId
            ? () => openActionRoute(MENTIONS_EXTRACT_ACTION_KIND)
            : undefined
        }
      />
    </WorkbenchContributionFrame>
  );
}

export function NotificationsWorkbenchPanel({
  host = 'leftSidebar',
}: NotificationsWorkbenchPanelProps) {
  const descriptor = NOTIFICATIONS_DESCRIPTOR;
  const placement = placementForHost(descriptor, host);

  return (
    <WorkbenchContributionFrame descriptor={descriptor} host={placement.host} className="">
      <NotificationsPanel />
    </WorkbenchContributionFrame>
  );
}

export function ViewsWorkbenchPanel({
  host = 'leftSidebar',
  ...props
}: ViewsWorkbenchPanelProps) {
  const descriptor = VIEWS_DESCRIPTOR;
  const placement = placementForHost(descriptor, host);

  return (
    <WorkbenchContributionFrame descriptor={descriptor} host={placement.host} className="">
      <ViewsPanel {...props} />
    </WorkbenchContributionFrame>
  );
}

export function AlertsWorkbenchPanel({
  canEdit,
  canCreateFromCurrentView = false,
  host = 'leftSidebar',
  onCreateFromCurrentView = () => {},
}: AlertsWorkbenchPanelProps) {
  const descriptor = ALERTS_DESCRIPTOR;
  const placement = placementForHost(descriptor, host);

  return (
    <WorkbenchContributionFrame
      descriptor={descriptor}
      host={placement.host}
      className={placement.host === 'bottomDock' ? 'bottom-dock-tab-body' : ''}
    >
      <AlertsPanel
        canEdit={canEdit}
        canCreateFromCurrentView={canCreateFromCurrentView}
        onCreateFromCurrentView={onCreateFromCurrentView}
      />
    </WorkbenchContributionFrame>
  );
}

export function EmbeddingsWorkbenchPanel({
  apiPort,
  projectId,
  host = 'leftSidebar',
  sheet,
  onSelectSheet,
  onOpenView,
  selectedRowIds,
  onFindSimilar,
}: EmbeddingsWorkbenchPanelProps) {
  const descriptor = EMBEDDINGS_DESCRIPTOR;
  const placement = placementForHost(descriptor, host);

  return (
    <WorkbenchContributionFrame descriptor={descriptor} host={placement.host} className="">
      <EmbeddingsPanel
        apiPort={apiPort}
        projectId={projectId}
        sheet={sheet}
        onSelectSheet={onSelectSheet}
        onOpenView={onOpenView}
        selectedRowIds={selectedRowIds}
        onFindSimilar={onFindSimilar}
      />
    </WorkbenchContributionFrame>
  );
}

export function FeedsWorkbenchPanel({
  host = 'leftSidebar',
  feedApi,
  onPolled,
  onOpenFeedHealthMainView,
  onOpenImportDialog,
  feedDetailContributions,
  renderPluginDetailTab,
}: FeedsWorkbenchPanelProps) {
  const descriptor = FEEDS_DESCRIPTOR;
  const placement = placementForHost(descriptor, host);
  // The panel's Add affordance reuses the EXISTING Import workspace dialog
  // (its "Feed" mode owns source creation) via the shared actSurface store --
  // no App.tsx wiring needed, the same mechanism the ribbon's Import command
  // and core/commands/first-party/importOpen use
  // (ctx.actSurface.openImportDialog()). This entry point forces the dialog
  // onto the Feed step specifically ("sources should be feed, that's what
  // sources are for") — the generic Import entry points (ribbon, ⌘K, Ask
  // handoff) call openImportDialog() with no mode and keep preserving
  // whatever mode the user last had selected.
  const actSurface = useActSurfaceHandle();
  const openImportDialogOnFeed = () => actSurface.openImportDialog('feed');

  return (
    <WorkbenchContributionFrame descriptor={descriptor} host={placement.host} className="">
      <FeedsPanel
        feedApi={feedApi}
        onPolled={onPolled}
        onOpenFeedHealthMainView={onOpenFeedHealthMainView}
        onOpenImportDialog={onOpenImportDialog ?? openImportDialogOnFeed}
        feedKindFormFrame={FeedKindWorkbenchFormFrame}
        feedDetailFrames={{
          Summary: FeedDetailSummaryTabFrame,
          Health: FeedDetailHealthTabFrame,
          Runs: FeedDetailChecksTabFrame,
        }}
        feedDetailContributions={feedDetailContributions}
        renderPluginDetailTab={renderPluginDetailTab}
      />
    </WorkbenchContributionFrame>
  );
}

export function HistoryWorkbenchPanel({
  host = 'bottomDock',
  history,
  onRevert,
  onRestore,
  onLoadPage,
}: HistoryWorkbenchPanelProps) {
  const descriptor = HISTORY_DESCRIPTOR;
  const placement = placementForHost(descriptor, host);

  return (
    <WorkbenchContributionFrame
      descriptor={descriptor}
      host={placement.host}
      className={placement.host === 'bottomDock' ? 'bottom-dock-tab-body' : ''}
    >
      <HistoryPanel history={history} onRevert={onRevert} onRestore={onRestore} onLoadPage={onLoadPage} />
    </WorkbenchContributionFrame>
  );
}
