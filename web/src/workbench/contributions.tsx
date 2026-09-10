import type { ReactNode } from 'react';
import type { CopilotProposal } from '../api/open';
import { useActSurfaceHandle } from '../bind/useActSurfaceHandle';
import { CopilotPanel } from '../components/CopilotPanel';
import { EmbeddingsPanel, type EmbeddingsPanelProps } from '../components/EmbeddingsPanel';
import { FriendlyFiltersPanel, type FriendlyFiltersPanelProps } from '../components/FriendlyFiltersPanel';
import { HistoryPanel, type HistoryPanelProps } from '../components/HistoryPanel';
import { MentionsPanel, type MentionsPanelProps } from '../components/MentionsPanel';
import { MENTIONS_EXTRACT_ACTION_KIND } from '../components/mentionsPanelModel';
import { NotificationsPanel } from '../components/NotificationsPanel';
import { SearchPanel, type SearchPanelProps } from '../components/SearchPanel';
import { SourcesPanel, type SourcesPanelProps } from '../components/SourcesPanel';
import { WatchesPanel } from '../components/WatchesPanel';
import { ViewsPanel, type ViewsPanelProps } from '../workspace/ViewsPanel';
import {
  hostContextCapabilityAttribute,
  type WorkbenchHostContext,
} from './hostContext';
import {
  COLUMN_RUNS_SECTION_DESCRIPTOR,
  COLUMN_SETTINGS_SECTION_DESCRIPTOR,
  COPILOT_DESCRIPTOR,
  COST_GATE_VIEW_DESCRIPTOR,
  EMBEDDINGS_DESCRIPTOR,
  ENTITY_CONNECTIONS_VIEW_DESCRIPTOR,
  ENTITY_EVIDENCE_VIEW_DESCRIPTOR,
  FRIENDLY_FILTERS_DESCRIPTOR,
  ENTITY_SUMMARY_VIEW_DESCRIPTOR,
  EVIDENCE_VIEW_DESCRIPTOR,
  GRAPH_NEIGHBORHOOD_VIEW_DESCRIPTOR,
  GRID_VIEW_DESCRIPTOR,
  HISTORY_DESCRIPTOR,
  IMAGE_GALLERY_VIEW_DESCRIPTOR,
  MENTIONS_DESCRIPTOR,
  NOTIFICATIONS_DESCRIPTOR,
  OUTPUT_COLUMN_COLLISION_VIEW_DESCRIPTOR,
  PROVENANCE_PANEL_DESCRIPTOR,
  REVIEW_QUEUE_VIEW_DESCRIPTOR,
  ROW_DELETE_CONFIRM_VIEW_DESCRIPTOR,
  ROW_INSPECTOR_EVIDENCE_SECTION_DESCRIPTOR,
  SAVED_VIEWS_DESCRIPTOR,
  SEARCH_DESCRIPTOR,
  SOURCES_DESCRIPTOR,
  SOURCE_HEALTH_VIEW_DESCRIPTOR,
  SOURCE_KIND_FORM_DESCRIPTORS,
  SOURCE_RUNS_VIEW_DESCRIPTOR,
  SOURCE_SUMMARY_VIEW_DESCRIPTOR,
  WATCHES_DESCRIPTOR,
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
          data-testid={`workbench-resolved-layout-item-${item.contributionId.replace(/[^a-zA-Z0-9]+/g, '-')}`}
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

interface SearchWorkbenchPanelProps extends SearchPanelProps {
  host?: WorkbenchRegionId;
}

interface CopilotWorkbenchPanelProps {
  host?: WorkbenchRegionId;
  onRunProposal(proposal: CopilotProposal): Promise<boolean>;
  onInspectProposal(proposal: CopilotProposal): void;
  onImportNeeded?(): void;
  onClose?(): void;
  /** Header-only strip state, owned by the popover host. */
  collapsed?: boolean;
  onCollapsedChange?(collapsed: boolean): void;
}

interface NotificationsWorkbenchPanelProps {
  host?: WorkbenchRegionId;
  projectId: string;
}

interface SavedViewsWorkbenchPanelProps extends ViewsPanelProps {
  host?: WorkbenchRegionId;
}

interface WatchesWorkbenchPanelProps {
  canEdit: boolean;
  canCreateFromCurrentView?: boolean;
  host?: WorkbenchRegionId;
  onCreateFromCurrentView?(): void;
}

interface EmbeddingsWorkbenchPanelProps extends EmbeddingsPanelProps {
  host?: WorkbenchRegionId;
}

interface SourcesWorkbenchPanelProps extends SourcesPanelProps {
  host?: WorkbenchRegionId;
}

interface HistoryWorkbenchPanelProps extends HistoryPanelProps {
  host?: WorkbenchRegionId;
}

function contributionTestId(contributionId: string): string {
  return `workbench-contribution-${contributionId.replace(/[^a-zA-Z0-9]+/g, '-')}`;
}

function commandTestId(contributionId: string): string {
  return `workbench-command-${contributionId.replace(/[^a-zA-Z0-9]+/g, '-')}`;
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

export function RowInspectorEvidenceSectionFrame({ children }: { children: ReactNode }) {
  const rowDetailPlacement = placementForHost(ROW_INSPECTOR_EVIDENCE_SECTION_DESCRIPTOR, 'rowDetail');
  return (
    <WorkbenchContributionFrame
      descriptor={ROW_INSPECTOR_EVIDENCE_SECTION_DESCRIPTOR}
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

export function EvidenceWorkbenchViewFrame({
  host = 'modalOrPeek',
  children,
}: {
  host?: WorkbenchHostId;
  children: ReactNode;
}) {
  return (
    <WorkbenchContributionFrame descriptor={EVIDENCE_VIEW_DESCRIPTOR} host={host}>
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

export function OutputColumnCollisionWorkbenchViewFrame({
  children,
}: {
  children: ReactNode;
}) {
  return (
    <WorkbenchContributionFrame
      descriptor={OUTPUT_COLUMN_COLLISION_VIEW_DESCRIPTOR}
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

export function EntityDetailEvidenceTabFrame({ children }: { children: ReactNode }) {
  return (
    <WorkbenchContributionFrame
      descriptor={ENTITY_EVIDENCE_VIEW_DESCRIPTOR}
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

export function SourceDetailSummaryTabFrame({ children }: { children: ReactNode }) {
  return (
    <WorkbenchContributionFrame
      descriptor={SOURCE_SUMMARY_VIEW_DESCRIPTOR}
      host="sourceDetail"
      className="source-detail-contribution-tab"
    >
      {children}
    </WorkbenchContributionFrame>
  );
}

export function SourceDetailHealthTabFrame({ children }: { children: ReactNode }) {
  return (
    <WorkbenchContributionFrame
      descriptor={SOURCE_HEALTH_VIEW_DESCRIPTOR}
      host="sourceDetail"
      className="source-detail-contribution-tab"
    >
      {children}
    </WorkbenchContributionFrame>
  );
}

export function SourceHealthMainViewFrame({ children }: { children: ReactNode }) {
  return (
    <WorkbenchContributionFrame
      descriptor={SOURCE_HEALTH_VIEW_DESCRIPTOR}
      host="mainView"
    >
      {children}
    </WorkbenchContributionFrame>
  );
}

export function SourceDetailRunsTabFrame({ children }: { children: ReactNode }) {
  return (
    <WorkbenchContributionFrame
      descriptor={SOURCE_RUNS_VIEW_DESCRIPTOR}
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

export function SourceKindWorkbenchFormFrame({
  sourceKind,
  children,
}: {
  sourceKind: string;
  children: ReactNode;
}) {
  const descriptor = SOURCE_KIND_FORM_DESCRIPTORS[sourceKind];
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

export function SearchWorkbenchPanel({
  host = 'leftSidebar',
  sheets,
  onPick,
}: SearchWorkbenchPanelProps) {
  const descriptor = SEARCH_DESCRIPTOR;
  const placement = placementForHost(descriptor, host);

  return (
    <WorkbenchContributionFrame descriptor={descriptor} host={placement.host} className="">
      <SearchPanel sheets={sheets} onPick={onPick} />
    </WorkbenchContributionFrame>
  );
}

export function CopilotWorkbenchPanel({
  host = 'leftSidebar',
  onRunProposal,
  onInspectProposal,
  onImportNeeded,
  onClose,
  collapsed,
  onCollapsedChange,
}: CopilotWorkbenchPanelProps) {
  const descriptor = COPILOT_DESCRIPTOR;
  const placement = placementForHost(descriptor, host);

  return (
    <WorkbenchContributionFrame descriptor={descriptor} host={placement.host} className="">
      <CopilotPanel
        onRunProposal={onRunProposal}
        onInspectProposal={onInspectProposal}
        onImportNeeded={onImportNeeded}
        onClose={onClose}
        collapsed={collapsed}
        onCollapsedChange={onCollapsedChange}
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

export function SavedViewsWorkbenchPanel({
  host = 'leftSidebar',
  ...props
}: SavedViewsWorkbenchPanelProps) {
  const descriptor = SAVED_VIEWS_DESCRIPTOR;
  const placement = placementForHost(descriptor, host);

  return (
    <WorkbenchContributionFrame descriptor={descriptor} host={placement.host} className="">
      <ViewsPanel {...props} />
    </WorkbenchContributionFrame>
  );
}

export function WatchesWorkbenchPanel({
  canEdit,
  canCreateFromCurrentView = false,
  host = 'leftSidebar',
  onCreateFromCurrentView = () => {},
}: WatchesWorkbenchPanelProps) {
  const descriptor = WATCHES_DESCRIPTOR;
  const placement = placementForHost(descriptor, host);

  return (
    <WorkbenchContributionFrame
      descriptor={descriptor}
      host={placement.host}
      className={placement.host === 'bottomDock' ? 'bottom-dock-tab-body' : ''}
    >
      <WatchesPanel
        canEdit={canEdit}
        canCreateFromCurrentView={canCreateFromCurrentView}
        onCreateFromCurrentView={onCreateFromCurrentView}
      />
    </WorkbenchContributionFrame>
  );
}

export function EmbeddingsWorkbenchPanel({
  apiPort,
  host = 'leftSidebar',
  sheet,
  onSelectSheet,
  onOpenLens,
}: EmbeddingsWorkbenchPanelProps) {
  const descriptor = EMBEDDINGS_DESCRIPTOR;
  const placement = placementForHost(descriptor, host);

  return (
    <WorkbenchContributionFrame descriptor={descriptor} host={placement.host} className="">
      <EmbeddingsPanel
        apiPort={apiPort}
        sheet={sheet}
        onSelectSheet={onSelectSheet}
        onOpenLens={onOpenLens}
      />
    </WorkbenchContributionFrame>
  );
}

export function SourcesWorkbenchPanel({
  host = 'leftSidebar',
  sourceApi,
  onPolled,
  onOpenSourceHealthMainView,
  onOpenImportDialog,
  sourceDetailContributions,
  renderPluginDetailTab,
}: SourcesWorkbenchPanelProps) {
  const descriptor = SOURCES_DESCRIPTOR;
  const placement = placementForHost(descriptor, host);
  // The panel's Add affordance reuses the EXISTING Import workspace dialog
  // (its "Feed" mode owns source creation) via the shared actSurface store --
  // no App.tsx wiring needed, the same mechanism the ribbon's Import command
  // and core/commands/first-party/importOpen use
  // (ctx.actSurface.openImportDialog()). This entry point forces the dialog
  // onto the Feed step specifically ("sources should be feed, that's what
  // sources are for") — the generic Import entry points (ribbon, ⌘K, Copilot
  // handoff) call openImportDialog() with no mode and keep preserving
  // whatever mode the user last had selected.
  const actSurface = useActSurfaceHandle();
  const openImportDialogOnFeed = () => actSurface.openImportDialog('feed');

  return (
    <WorkbenchContributionFrame descriptor={descriptor} host={placement.host} className="">
      <SourcesPanel
        sourceApi={sourceApi}
        onPolled={onPolled}
        onOpenSourceHealthMainView={onOpenSourceHealthMainView}
        onOpenImportDialog={onOpenImportDialog ?? openImportDialogOnFeed}
        sourceKindFormFrame={SourceKindWorkbenchFormFrame}
        sourceDetailFrames={{
          Summary: SourceDetailSummaryTabFrame,
          Health: SourceDetailHealthTabFrame,
          Runs: SourceDetailRunsTabFrame,
        }}
        sourceDetailContributions={sourceDetailContributions}
        renderPluginDetailTab={renderPluginDetailTab}
      />
    </WorkbenchContributionFrame>
  );
}

export function HistoryWorkbenchPanel({
  host = 'bottomDock',
  history,
  onStepTo,
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
      <HistoryPanel history={history} onStepTo={onStepTo} onLoadPage={onLoadPage} />
    </WorkbenchContributionFrame>
  );
}
