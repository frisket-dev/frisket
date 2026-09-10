// First-party descriptor DATA is a checked-in JSON package validated by the same
// backend loader plugin packages go through
// (workbench/contracts.py::load_workbench_descriptor_package_file). This is a
// package. The frontend copy keeps the independently packable frontend host
// self-contained; the backend contract test owns semantic parity with the
// Python package asset.
import firstPartyWorkbenchDescriptorPackage from '../assets/first_party_workbench_descriptors.json' with { type: 'json' };
import {
  firstPartyWorkbenchDescriptorsFromPackage,
  type FirstPartyWorkbenchDescriptorPackage,
} from './pluginRuntimeDescriptors';

export type WorkbenchRegionId =
  | 'activityRail'
  | 'leftSidebar'
  | 'mainView'
  | 'rightInspector'
  | 'bottomDock'
  | 'modalOrPeek';

export type WorkbenchHostId =
  | WorkbenchRegionId
  | 'rowDetail'
  | 'entityDetail'
  | 'sourceDetail'
  | 'columnDetail'
  | 'rowInspector'
  | 'columnInspector'
  | 'commandPalette';

export type WorkbenchPlacementMode = 'panel' | 'pane' | 'tab' | 'peek' | 'section' | 'command';

export interface WorkbenchPlacement {
  host: WorkbenchHostId;
  mode: WorkbenchPlacementMode;
  placementId?: string;
  slot?: WorkbenchPlacementSlot;
  default?: boolean;
  order?: number;
  tabChrome?: {
    closeable?: boolean;
    singleton?: boolean;
  };
  density?: 'compact' | 'comfortable' | 'wide';
  minSize?: number;
  defaultSize?: number;
}

export interface WorkbenchRequirement {
  kind: 'hostCapability' | 'permission' | 'dataRequirement' | 'contribution';
  id?: string;
  optional?: boolean;
}

export type WorkbenchPlacementSlot =
  | 'scope'
  | 'work.primary'
  | 'work.companion'
  | 'inspection'
  | 'configuration'
  | 'companion.output'
  | 'interruption'
  | 'launcher'
  | 'detail';

export interface WorkbenchDataRequirement {
  kind:
    | 'activeProject'
    | 'activeSheet'
    | 'activeRow'
    | 'activeColumn'
    | 'activeCell'
    | 'activeEvidence'
    | 'activeSource'
    | 'activeEntity'
    | 'activeProjection'
    | 'selectedRows'
    | 'sheetHasColumnType';
  id?: string;
  columnType?: string;
  min?: number;
  optional?: boolean;
}

interface WorkbenchContributionDescriptorBase {
  schemaVersion:
    | 'frisket.workbench.panel.v1'
    | 'frisket.workbench.view.v1'
    | 'frisket.row_inspector.section.v1'
    | 'frisket.column_inspector.section.v1'
    | 'frisket.command.v1'
    | 'frisket.source.kind.form.v1'
    | 'frisket.action.form.v1';
  id: string;
  kind: 'panel' | 'view' | 'inspectorSection' | 'command' | 'provider' | 'actionForm';
  ownerPluginId: string;
  title: string;
  shortTitle?: string;
  icon?: string;
  componentKey: string;
  runtimeComponent?: {
    pluginId: string;
    contributionId: string;
    componentKey: string;
    moduleUrl: string;
    moduleKey: string;
    manifestSha256: string;
    packageSha256: string;
  };
  placements: WorkbenchPlacement[];
  requires: WorkbenchRequirement[];
  dataRequirements?: WorkbenchDataRequirement[];
}

export interface WorkbenchPanelDescriptor extends WorkbenchContributionDescriptorBase {
  schemaVersion: 'frisket.workbench.panel.v1';
  kind: 'panel';
}

export interface WorkbenchViewDescriptor extends WorkbenchContributionDescriptorBase {
  schemaVersion: 'frisket.workbench.view.v1';
  kind: 'view';
  projectionKind?: string;
  projectionParams?: Record<string, unknown>;
}

export interface RowInspectorSectionDescriptor extends WorkbenchContributionDescriptorBase {
  schemaVersion: 'frisket.row_inspector.section.v1';
  kind: 'inspectorSection';
}

export interface ColumnInspectorSectionDescriptor extends WorkbenchContributionDescriptorBase {
  schemaVersion: 'frisket.column_inspector.section.v1';
  kind: 'inspectorSection';
}

export interface WorkbenchCommandDescriptor extends WorkbenchContributionDescriptorBase {
  schemaVersion: 'frisket.command.v1';
  kind: 'command';
  commandId: string;
  handlerKey: string;
}

export interface SourceKindFormDescriptor extends WorkbenchContributionDescriptorBase {
  schemaVersion: 'frisket.source.kind.form.v1';
  kind: 'provider';
  accepts: string[];
}

export interface ActionFormDescriptor extends WorkbenchContributionDescriptorBase {
  schemaVersion: 'frisket.action.form.v1';
  kind: 'actionForm';
  actionKind: string;
  modes: string[];
}

export type WorkbenchContributionDescriptor =
  | WorkbenchPanelDescriptor
  | WorkbenchViewDescriptor
  | RowInspectorSectionDescriptor
  | ColumnInspectorSectionDescriptor
  | WorkbenchCommandDescriptor
  | SourceKindFormDescriptor
  | ActionFormDescriptor;

export type WorkbenchResolvedPlacement = WorkbenchPlacement & {
  placementId: string;
  slot: WorkbenchPlacementSlot;
};

export const WORKBENCH_SLOT_BY_HOST: Record<WorkbenchHostId, WorkbenchPlacementSlot> = {
  activityRail: 'launcher',
  leftSidebar: 'scope',
  mainView: 'work.primary',
  rightInspector: 'inspection',
  bottomDock: 'companion.output',
  modalOrPeek: 'interruption',
  rowDetail: 'detail',
  entityDetail: 'detail',
  sourceDetail: 'detail',
  columnDetail: 'detail',
  rowInspector: 'detail',
  columnInspector: 'detail',
  commandPalette: 'launcher',
};

const WORKBENCH_LEGAL_SLOTS = new Set<WorkbenchPlacementSlot>([
  'scope',
  'work.primary',
  'work.companion',
  'inspection',
  'configuration',
  'companion.output',
  'interruption',
  'launcher',
  'detail',
]);

// A view descriptor is a projection view iff it declares projectionKind; which
// component renders it is a separate binding concern (runtime module or a
// host-component registry lookup), never a componentKey comparison.
export function isProjectionViewDescriptor(descriptor: WorkbenchViewDescriptor): boolean {
  return typeof descriptor.projectionKind === 'string' && descriptor.projectionKind.length > 0;
}

function placementIdForPlacement(
  descriptor: WorkbenchContributionDescriptor,
  placement: WorkbenchPlacement,
): string {
  return (
    placement.placementId ??
    `${descriptor.id}:${placement.host}:${placement.mode}:${placement.order ?? 0}`
  );
}

function slotForPlacement(placement: WorkbenchPlacement): WorkbenchPlacementSlot {
  const slot = placement.slot ?? WORKBENCH_SLOT_BY_HOST[placement.host];
  if (!WORKBENCH_LEGAL_SLOTS.has(slot)) {
    throw new Error(`unsupported workbench placement slot: ${slot}`);
  }
  return slot;
}

export function normalizePlacement(
  descriptor: WorkbenchContributionDescriptor,
  placement: WorkbenchPlacement,
): WorkbenchResolvedPlacement {
  return {
    ...placement,
    placementId: placementIdForPlacement(descriptor, placement),
    slot: slotForPlacement(placement),
  };
}

// Every exported descriptor const below is DERIVED from the checked-in JSON
// package, run through the same trust-parameterized parser plugin manifests
// use (pluginRuntimeDescriptors.ts, trust='firstParty'). componentKey and
// (for commands) handlerKey are the only fields the JSON omits — they come
// back from the registry seam (web/src/workbench/firstPartyComponents.ts).
// A missing id here is a build-time invariant violation, not a run-time
// trust boundary: fail loudly.
const FIRST_PARTY_DESCRIPTORS_FROM_PACKAGE: WorkbenchContributionDescriptor[] =
  firstPartyWorkbenchDescriptorsFromPackage(
    firstPartyWorkbenchDescriptorPackage as FirstPartyWorkbenchDescriptorPackage,
  );

const FIRST_PARTY_DESCRIPTOR_BY_ID = new Map<string, WorkbenchContributionDescriptor>(
  FIRST_PARTY_DESCRIPTORS_FROM_PACKAGE.map((descriptor) => [descriptor.id, descriptor]),
);

function firstPartyDescriptor<T extends WorkbenchContributionDescriptor>(id: string): T {
  const descriptor = FIRST_PARTY_DESCRIPTOR_BY_ID.get(id);
  if (!descriptor) {
    throw new Error(`missing first-party workbench descriptor package entry: ${id}`);
  }
  return descriptor as T;
}

// The full parsed set, in artifact order, stamped with runtimeSource by
// layout.ts at merge time (ONE merged stream, first-party-wins ordering —
// this array is listed first in that merge).
export const FIRST_PARTY_WORKBENCH_CONTRIBUTION_DESCRIPTORS: readonly WorkbenchContributionDescriptor[] =
  FIRST_PARTY_DESCRIPTORS_FROM_PACKAGE;

// Frontend first-party descriptors are consumed by their exported frame/wrapper
// components below; this module does not maintain a separate descriptor inventory.
export const FRIENDLY_FILTERS_DESCRIPTOR: WorkbenchPanelDescriptor = firstPartyDescriptor(
  'frisket.investigative.panel.friendly_filters',
);

// Mentions is a SEPARATE panel beside Facets: Facets is a
// distinct-value browser, Mentions browses fingerprint-grouped named-entity
// mentions.
export const MENTIONS_DESCRIPTOR: WorkbenchPanelDescriptor = firstPartyDescriptor(
  'frisket.investigative.panel.mentions',
);

export const SOURCES_DESCRIPTOR: WorkbenchPanelDescriptor = firstPartyDescriptor(
  'frisket.core.panel.sources',
);

export const SEARCH_DESCRIPTOR: WorkbenchPanelDescriptor = firstPartyDescriptor(
  'frisket.core.panel.search',
);

export const COPILOT_DESCRIPTOR: WorkbenchPanelDescriptor = firstPartyDescriptor(
  'frisket.core.panel.copilot',
);

export const NOTIFICATIONS_DESCRIPTOR: WorkbenchPanelDescriptor = firstPartyDescriptor(
  'frisket.core.panel.notifications',
);

export const SAVED_VIEWS_DESCRIPTOR: WorkbenchPanelDescriptor = firstPartyDescriptor(
  'frisket.core.panel.saved_views',
);

export const WATCHES_DESCRIPTOR: WorkbenchPanelDescriptor = firstPartyDescriptor(
  'frisket.core.panel.watches',
);

export const EMBEDDINGS_DESCRIPTOR: WorkbenchPanelDescriptor = firstPartyDescriptor(
  'frisket.embeddings.panel.indexes',
);

export const SOURCE_KIND_FORM_DESCRIPTORS: Record<string, SourceKindFormDescriptor> = {
  rss: firstPartyDescriptor('frisket.core.source_kind_form.rss'),
  api_list_dicts: firstPartyDescriptor('frisket.core.source_kind_form.api_list_dicts'),
};

// No ACTIONS panel descriptor: actions are EXPRESSED by the shell as the
// overlay ActionDrawer (App.tsx WorkspaceActionDrawerRegion), never PLACED via
// a host/slot descriptor. The action CATALOG remains the contribution
// surface — dynamic per-kind actionFormDescriptor entries (below). (The
// frisket.core.command.run_actions palette command was removed with the
// non-functional custom-action authoring surface.)
function titleCaseActionKind(actionKind: string): string {
  return actionKind
    .split(/[^a-zA-Z0-9]+/)
    .filter(Boolean)
    .map((part) => `${part.charAt(0).toUpperCase()}${part.slice(1)}`)
    .join('');
}

// actionFormDescriptor is a dynamic factory (one per runtime action kind, not
// a fixed set) — it has no entry in the checked-in first-party descriptor
// package and is not part of that artifact/registry cutover.
export function actionFormDescriptor(actionKind: string, title?: string): ActionFormDescriptor {
  const componentStem = titleCaseActionKind(actionKind) || 'Action';
  return {
    schemaVersion: 'frisket.action.form.v1',
    id: `frisket.core.action_form.${actionKind.replace(/[^a-zA-Z0-9]+/g, '_')}`,
    kind: 'actionForm',
    ownerPluginId: 'frisket.core',
    title: title ?? `${componentStem} action`,
    icon: 'Play',
    actionKind,
    componentKey: `core.actions.${componentStem}Form`,
    modes: ['run', 'preview', 'proposalInspect'],
    placements: [
      { host: 'modalOrPeek', mode: 'peek', default: true, order: 10 },
    ],
    requires: [
      { kind: 'hostCapability', id: 'action.run' },
      { kind: 'hostCapability', id: 'action.estimate', optional: true },
      { kind: 'permission', id: 'project.write' },
    ],
  };
}

export const HISTORY_DESCRIPTOR: WorkbenchPanelDescriptor = firstPartyDescriptor(
  'frisket.core.panel.history',
);

export const BOTTOM_DOCK_LINEAGE_DESCRIPTOR: WorkbenchPanelDescriptor = firstPartyDescriptor(
  'frisket.core.panel.lineage',
);

// No projection_status panel descriptor: the panel retired to a status-bar
// chip (visibility.ts) and its descriptor trio (this export, the
// firstPartyComponents binding, the JSON artifact entry) was removed — the
// bound component (core.panels.ProjectionStatusPanel) never existed.

// No PLUGIN_MANAGER_DESCRIPTOR: the frisket.core.panel.plugins dock tab
// retired — it was a read-only duplicate of the full manager Settings
// already hosts (SettingsSections.tsx's ProjectPluginsSettings renders the
// identical PluginManager component with mode="settings"). Failures now
// route into the Errors dock tab (dockJobSummary.ts's derivePluginErrorJobs)
// and health into Diagnostics; the descriptor entry was removed from the
// checked-in artifact (src/frisket/data/first_party_workbench_descriptors.json)
// and its firstPartyComponents.ts binding, same disposal as the
// projection_status panel above.

export const BOTTOM_DOCK_JOBS_DESCRIPTOR: WorkbenchPanelDescriptor = firstPartyDescriptor(
  'frisket.core.panel.jobs',
);

export const BOTTOM_DOCK_ERRORS_DESCRIPTOR: WorkbenchPanelDescriptor = firstPartyDescriptor(
  'frisket.core.panel.errors',
);

export const OPEN_SOURCES_COMMAND_DESCRIPTOR: WorkbenchCommandDescriptor = firstPartyDescriptor(
  'frisket.core.command.open_sources',
);

// Settings re-home: the retired sidebar footer's SettingsLink lands as a
// palette command (its other new home is the project ▾ menu). The chrome bar
// carries no gear.
export const OPEN_SETTINGS_COMMAND_DESCRIPTOR: WorkbenchCommandDescriptor = firstPartyDescriptor(
  'frisket.core.command.open_settings',
);

// OCR Compare bake-off: the scratch center tab's ⌘K entry — the same
// command the ribbon Read-tab tile and the Read menu-bar item dispatch.
export const OCR_COMPARE_COMMAND_DESCRIPTOR: WorkbenchCommandDescriptor = firstPartyDescriptor(
  'frisket.media.command.ocr_compare',
);

// Transcription Compare bake-off: the scratch center tab's ⌘K entry — the
// same command the ribbon Read-tab tile and the Read menu-bar item dispatch
// (the OCR Compare trio pattern).
export const TRANSCRIBE_COMPARE_COMMAND_DESCRIPTOR: WorkbenchCommandDescriptor =
  firstPartyDescriptor('frisket.media.command.transcribe_compare');

// Translate Compare bake-off: the text-source scratch center tab's ⌘K entry
// — same command the ribbon Read-tab tile dispatches (the OCR/Transcribe
// Compare pattern).
export const TRANSLATE_COMPARE_COMMAND_DESCRIPTOR: WorkbenchCommandDescriptor =
  firstPartyDescriptor('frisket.media.command.translate_compare');

export const TOPIC_COMPARE_COMMAND_DESCRIPTOR: WorkbenchCommandDescriptor =
  firstPartyDescriptor('frisket.media.command.topic_segmentation_compare');

export const GRID_VIEW_DESCRIPTOR: WorkbenchViewDescriptor = firstPartyDescriptor(
  'frisket.core.view.grid',
);

// The Map view descriptor is PLUGIN-OWNED: it ships in the bundled
// frisket.geo package's workbench-descriptors.json
// (src/frisket/authoring/bundled_plugins/frisket.geo/) and arrives through the runtime
// index like any other plugin contribution — there is no first-party
// MAP_VIEW_DESCRIPTOR anymore.

export const IMAGE_GALLERY_VIEW_DESCRIPTOR: WorkbenchViewDescriptor = firstPartyDescriptor(
  'frisket.media.view.image_gallery',
);

export const EVIDENCE_VIEW_DESCRIPTOR: WorkbenchViewDescriptor = firstPartyDescriptor(
  'frisket.core.view.evidence',
);

export const REVIEW_QUEUE_VIEW_DESCRIPTOR: WorkbenchViewDescriptor = firstPartyDescriptor(
  'frisket.core.view.review_queue',
);

export const PROVENANCE_PANEL_DESCRIPTOR: WorkbenchPanelDescriptor = firstPartyDescriptor(
  'frisket.core.panel.provenance',
);

export const ROW_DELETE_CONFIRM_VIEW_DESCRIPTOR: WorkbenchViewDescriptor = firstPartyDescriptor(
  'frisket.core.view.row_delete_confirm',
);

export const COST_GATE_VIEW_DESCRIPTOR: WorkbenchViewDescriptor = firstPartyDescriptor(
  'frisket.core.view.cost_gate',
);

// The server-side output_column_exists confirm modal — same modalOrPeek
// host as cost_gate/row_delete_confirm above.
export const OUTPUT_COLUMN_COLLISION_VIEW_DESCRIPTOR: WorkbenchViewDescriptor =
  firstPartyDescriptor('frisket.core.view.output_column_collision');

export const GRAPH_NEIGHBORHOOD_VIEW_DESCRIPTOR: WorkbenchViewDescriptor = firstPartyDescriptor(
  'frisket.investigative.view.graph_neighborhood',
);

export const ROW_INSPECTOR_EVIDENCE_SECTION_DESCRIPTOR: RowInspectorSectionDescriptor =
  firstPartyDescriptor('frisket.core.row_inspector.section.evidence');

export const COLUMN_SETTINGS_SECTION_DESCRIPTOR: ColumnInspectorSectionDescriptor =
  firstPartyDescriptor('frisket.core.column_inspector.section.settings');

export const COLUMN_RUNS_SECTION_DESCRIPTOR: ColumnInspectorSectionDescriptor = firstPartyDescriptor(
  'frisket.core.column_inspector.section.runs',
);

export const ENTITY_SUMMARY_VIEW_DESCRIPTOR: WorkbenchViewDescriptor = firstPartyDescriptor(
  'frisket.investigative.view.entity_summary',
);

export const ENTITY_EVIDENCE_VIEW_DESCRIPTOR: WorkbenchViewDescriptor = firstPartyDescriptor(
  'frisket.investigative.view.entity_evidence',
);

export const ENTITY_CONNECTIONS_VIEW_DESCRIPTOR: WorkbenchViewDescriptor = firstPartyDescriptor(
  'frisket.investigative.view.entity_connections',
);

export const SOURCE_SUMMARY_VIEW_DESCRIPTOR: WorkbenchViewDescriptor = firstPartyDescriptor(
  'frisket.core.view.source_summary',
);

export const SOURCE_HEALTH_VIEW_DESCRIPTOR: WorkbenchViewDescriptor = firstPartyDescriptor(
  'frisket.core.view.source_health',
);

export const SOURCE_RUNS_VIEW_DESCRIPTOR: WorkbenchViewDescriptor = firstPartyDescriptor(
  'frisket.core.view.source_runs',
);
