import type { ColumnDef, SheetMeta } from '../api/types';
import type {
  WorkbenchContributionDescriptor,
  WorkbenchPanelDescriptor,
} from './descriptors';
import type { WorkbenchHostContext } from './hostContext';
import type { DeckglNamespace } from '../components/map/deckglNamespace';

// Every plugin host context composes its sections from this fragment layer — no per-file
// duplicated section builders survive beside a fragment.

export interface SheetSnapshotFragment {
  id: string;
  name: string;
  rowCount: number;
  columns: ColumnDef[];
}

export function buildSheetSnapshotFragment(
  sheet: SheetMeta,
  columns: ColumnDef[] = sheet.columns,
): SheetSnapshotFragment {
  return {
    id: sheet.id,
    name: sheet.name,
    rowCount: sheet.rowCount,
    columns,
  };
}

export interface SelectionFragment {
  selectedRowIds: string[];
  selectedCount: number;
  activeRowId: string | null;
}

export function buildSelectionFragment(
  hostContext: WorkbenchHostContext,
): SelectionFragment {
  return {
    selectedRowIds: [...hostContext.selection.selectedRowIds],
    selectedCount: hostContext.selection.selectedRowIds.length,
    activeRowId: hostContext.selection.activeRowId,
  };
}

export interface PanelNavigationFragment {
  openRow(rowId: string): void;
}

function buildPanelNavigationFragment(
  descriptor: WorkbenchPanelDescriptor,
  sheet: SheetMeta,
  hostContext: WorkbenchHostContext,
): PanelNavigationFragment {
  return {
    openRow: (rowId) => {
      window.__FRISKET_PLUGIN_PANEL_TEST_SPIES__?.openRow?.({
        contributionId: descriptor.id,
        rowId,
      });
      hostContext.navigation.openRow(sheet.id, rowId);
    },
  };
}

// The panel-shaped composite every panel-family context (right-inspector
// panel, dock tab, detail, peek) starts from.
export interface PanelShapedSections {
  projectId: string;
  contributionId: string;
  sheet: SheetSnapshotFragment;
  selection: SelectionFragment;
  grid: {
    filter: WorkbenchHostContext['gridState']['filter'];
    sort: WorkbenchHostContext['gridState']['sort'];
  };
  navigation: PanelNavigationFragment;
}

export function buildPanelShapedSections({
  descriptor,
  sheet,
  hostContext,
}: {
  descriptor: WorkbenchPanelDescriptor;
  sheet: SheetMeta;
  hostContext: WorkbenchHostContext;
}): PanelShapedSections {
  return {
    projectId: hostContext.identity.projectId,
    contributionId: descriptor.id,
    sheet: buildSheetSnapshotFragment(sheet),
    selection: buildSelectionFragment(hostContext),
    grid: {
      filter: hostContext.gridState.filter,
      sort: hostContext.gridState.sort,
    },
    navigation: buildPanelNavigationFragment(descriptor, sheet, hostContext),
  };
}

// --- grid.state.read (capability-gated, optional on every host context) ---

const GRID_STATE_READ_CAPABILITY = 'grid.state.read';

export interface GridReadFragment {
  filter: WorkbenchHostContext['gridState']['filter'];
  sort: WorkbenchHostContext['gridState']['sort'];
  visibleColumnIds: string[];
  frozenColumnCount: number;
  activeLensId: number | null;
}

function buildGridReadFragment(
  hostContext: WorkbenchHostContext,
): GridReadFragment {
  return {
    filter: hostContext.gridState.filter,
    sort: hostContext.gridState.sort,
    visibleColumnIds: [...hostContext.gridState.columnOrder],
    frozenColumnCount: hostContext.gridState.frozenColumns,
    activeLensId: hostContext.gridState.lensId,
  };
}

// Exported: the shared gate every capability-gated optional context section
// (grid.state.read, action.run, grid.filter.applyBbox, and
// pluginProjectionViewContext.ts's projection.data.read) checks against —
// one gating rule, not a per-section reimplementation.
export function descriptorDeclaresCapability(
  descriptor: Pick<WorkbenchContributionDescriptor, 'requires'>,
  capabilityId: string,
): boolean {
  return descriptor.requires.some(
    (requirement) =>
      requirement.kind === 'hostCapability' && requirement.id === capabilityId,
  );
}

/** The optional ctx.gridState section: present IFF the descriptor declares
 *  the grid.state.read host capability (schemaVersions stay v1). */
export function optionalGridReadSection(
  descriptor: Pick<WorkbenchContributionDescriptor, 'requires'>,
  hostContext: WorkbenchHostContext,
): { gridState?: GridReadFragment } {
  if (!descriptorDeclaresCapability(descriptor, GRID_STATE_READ_CAPABILITY)) {
    return {};
  }
  return { gridState: buildGridReadFragment(hostContext) };
}

// --- action.run (capability-gated, optional on every host context) ---

const ACTION_RUN_CAPABILITY = 'action.run';

export interface ActionLaunchFragment {
  /** Host-mediated: awaits the REAL cost gate, then opens the action panel
   *  prefilled. Plugin code can never start a run silently. */
  run(actionKind: string): Promise<{ launched: boolean }>;
  /** Opens the action panel in preview mode for the kind. */
  preview(actionKind: string): Promise<{ launched: boolean }>;
}

function buildActionLaunchFragment({
  hostContext,
}: {
  hostContext: WorkbenchHostContext;
}): ActionLaunchFragment {
  return {
    run: async (actionKind) => {
      const confirmed = await hostContext.policy.confirmCost(actionKind);
      if (!confirmed) return { launched: false };
      await hostContext.actions.runAction(actionKind);
      return { launched: true };
    },
    preview: async (actionKind) => {
      await hostContext.actions.previewAction(actionKind);
      return { launched: true };
    },
  };
}

/** The optional ctx.actions section: present IFF the descriptor declares the
 *  action.run host capability. */
export function optionalActionLaunchSection(
  descriptor: Pick<WorkbenchContributionDescriptor, 'requires'>,
  hostContext: WorkbenchHostContext,
): { actions?: ActionLaunchFragment } {
  if (!descriptorDeclaresCapability(descriptor, ACTION_RUN_CAPABILITY)) {
    return {};
  }
  return { actions: buildActionLaunchFragment({ hostContext }) };
}

// --- grid.filter.applyBbox (capability-gated, optional on view host contexts) ---

const GRID_FILTER_APPLY_BBOX_CAPABILITY = 'grid.filter.applyBbox';

/** ctx.gridFilter — present IFF the descriptor declares hostCapability
 *  'grid.filter.applyBbox' (declared by the bundled geo plugin's map view).
 *  Applies the canonical geo-bbox grid filter through the host's real
 *  dispatch (hostContext.gridFilter.applyBbox -> applyGridBboxFilter, the
 *  "filter to this area" path) — one filter contract, no view-specific fork. */
export interface GridFilterApplyFragment {
  applyBbox(columnId: string, bbox: [number, number, number, number]): void;
}

function buildGridFilterApplyFragment(
  descriptor: Pick<WorkbenchContributionDescriptor, 'id'>,
  hostContext: WorkbenchHostContext,
): GridFilterApplyFragment {
  return {
    applyBbox: (columnId, bbox) => {
      window.__FRISKET_PLUGIN_VIEW_TEST_SPIES__?.applyBboxFilter?.({
        contributionId: descriptor.id,
        columnId,
        bbox,
      });
      hostContext.gridFilter.applyBbox(columnId, bbox);
    },
  };
}

/** The optional ctx.gridFilter section: present IFF the descriptor declares
 *  the grid.filter.applyBbox host capability. */
export function optionalGridFilterApplySection(
  descriptor: Pick<WorkbenchContributionDescriptor, 'id' | 'requires'>,
  hostContext: WorkbenchHostContext,
): { gridFilter?: GridFilterApplyFragment } {
  if (!descriptorDeclaresCapability(descriptor, GRID_FILTER_APPLY_BBOX_CAPABILITY)) {
    return {};
  }
  return { gridFilter: buildGridFilterApplyFragment(descriptor, hostContext) };
}

// --- host.library.deckgl (capability-gated, optional on view/projection-view
// host contexts) ---
//
// The React-injection precedent (TrustedLocalPluginComponent.tsx passes
// `React={React}` alongside `ctx`) generalized one notch for a SECOND
// host-provided library: unlike React, deck.gl is NOT eagerly bundled into
// every plugin-hosting page load (deckglNamespace.ts's ~780KB is far over
// the 256KB plugin-module cap this capability exists to work around), so it
// cannot ride a synchronous prop the way React does. It lives in ctx
// instead, as a Promise: ctx.libs.deckgl resolves to the deck.gl namespace
// the first time ANY declaring view mounts, dynamic-imported from the single
// deckglNamespace.ts module so the bytes ship in exactly one chunk. Building ctx
// itself therefore stays fully synchronous (useMemo, same as every other
// fragment here) — no new host-side pending/loading gate — and the plugin
// component awaits ctx.libs.deckgl itself, the same way it must already
// await TrustedLocalPluginComponent's own module load before it can render
// anything real.
const HOST_LIBRARY_DECKGL_CAPABILITY = 'host.library.deckgl';

export interface HostLibraryDeckglFragment {
  deckgl: Promise<DeckglNamespace>;
}

// Module-scoped: the dynamic import only fires once per page session no
// matter how many declaring views mount across the workspace's lifetime.
let deckglNamespacePromise: Promise<DeckglNamespace> | null = null;

function loadDeckglNamespace(): Promise<DeckglNamespace> {
  if (!deckglNamespacePromise) {
    deckglNamespacePromise = import('../components/map/deckglNamespace');
  }
  return deckglNamespacePromise;
}

/** The optional ctx.libs section: present IFF the descriptor declares the
 *  host.library.deckgl host capability. Deliberately ONE library behind a
 *  single flat `libs.deckgl` key, not a generic library registry (rule of
 *  three) — the shape is additive for a second library later. */
export function optionalHostLibrarySection(
  descriptor: Pick<WorkbenchContributionDescriptor, 'requires'>,
): { libs?: HostLibraryDeckglFragment } {
  if (!descriptorDeclaresCapability(descriptor, HOST_LIBRARY_DECKGL_CAPABILITY)) {
    return {};
  }
  return { libs: { deckgl: loadDeckglNamespace() } };
}
