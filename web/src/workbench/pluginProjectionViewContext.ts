import type {
  ColumnDef,
  RuntimeProjectionArtifactRef,
  RuntimeProjectionBuildMode,
  RuntimeProjectionBuildPlan,
  RuntimeProjectionStatus,
  RuntimeProjectionTarget,
  SheetMeta,
  TimelineProjectionArtifact,
} from '../api/types';
import type { PluginProjectionViewApiPort } from '../api/ports';
import {
  dataRequirementReason,
  firstMissingDataRequirement,
  sheetDataRequirementContext,
} from './dataRequirements';
import type { WorkbenchPlacementMode, WorkbenchViewDescriptor } from './descriptors';
import type { WorkbenchHostContext } from './hostContext';
import {
  buildSelectionFragment,
  buildSheetSnapshotFragment,
  descriptorDeclaresCapability,
  optionalActionLaunchSection,
  optionalGridFilterApplySection,
  optionalGridReadSection,
  optionalHostLibrarySection,
  type ActionLaunchFragment,
  type GridFilterApplyFragment,
  type GridReadFragment,
  type HostLibraryDeckglFragment,
  type SelectionFragment,
} from './pluginContextFragments';

export type PluginProjectionViewCapability =
  | 'projection.status'
  | 'projection.build'
  | 'projection.artifact.read'
  | 'projection.data.read'
  | 'host.navigation.openRow'
  | 'grid.state.read'
  | 'grid.filter.applyBbox'
  | 'action.run'
  | 'host.library.deckgl';

export type PluginProjectionViewApi = PluginProjectionViewApiPort;

/** ctx.projection.fetchData(params) — present IFF the descriptor declares
 *  hostCapability projection.data.read. Params mirror getMapPoints' query
 *  surface minus filter/sort (the host applies the workspace's current
 *  ctx.gridState filter/sort itself; the plugin never assembles a
 *  GridFilterSpec). columnId is the geo_point (or other projected) column to
 *  read points for. */
export interface ProjectionDataReadParams {
  columnId: string;
  bbox?: [number, number, number, number];
  attrs?: string[];
}

export interface PluginProjectionViewContext {
  schemaVersion: 'frisket.plugin_projection_view_context.v1';
  projectId: string;
  contributionId: string;
  placement: {
    host: 'mainView';
    mode: Extract<WorkbenchPlacementMode, 'pane'>;
  };
  sheet: {
    id: string;
    name: string;
    rowCount: number;
    columns: ColumnDef[];
  };
  /** The workspace's current row selection for the active sheet — same
   *  live state/semantics as PluginViewContext.selection. Always
   *  present (not capability-gated). */
  selection: SelectionFragment;
  projection: {
    kind: string;
    target: RuntimeProjectionTarget;
    params: Record<string, unknown>;
    status(): Promise<RuntimeProjectionStatus>;
    build(args?: { mode?: RuntimeProjectionBuildMode }): Promise<RuntimeProjectionBuildPlan>;
    readArtifact(ref: RuntimeProjectionArtifactRef): Promise<TimelineProjectionArtifact>;
    /** Present IFF the descriptor declares hostCapability
     *  projection.data.read. Arrow IPC bytes from the host-owned map-points
     *  service (the SAME route/service the first-party MapView reads
     *  through) — the plugin never sees the route. */
    fetchData?(params: ProjectionDataReadParams): Promise<ArrayBuffer>;
  };
  navigation: {
    openRow(rowId: string): void;
    /** Present IFF the HOST routed this view somewhere it can dismiss (e.g.
     *  the column-scoped map route): asks the host to close the view. Views
     *  mounted without a host route get no close affordance. */
    closeView?(): void;
  };
  /** Present IFF the descriptor declares hostCapability grid.state.read. */
  gridState?: GridReadFragment;
  /** Present IFF the descriptor declares hostCapability
   *  grid.filter.applyBbox — the SAME fragment plain views get
   *  (the geo map view is a projection view AND needs "filter to this
   *  area"). */
  gridFilter?: GridFilterApplyFragment;
  /** Present IFF the descriptor declares hostCapability action.run. */
  actions?: ActionLaunchFragment;
  /** Present IFF the descriptor declares hostCapability host.library.deckgl.
   *  Same shape/semantics as PluginViewContext.libs — see
   *  pluginContextFragments.ts optionalHostLibrarySection. */
  libs?: HostLibraryDeckglFragment;
}

export interface PluginProjectionViewUnavailable {
  available: false;
  reason: string;
}

export interface PluginProjectionViewAvailable {
  available: true;
  reason?: undefined;
}

export type PluginProjectionViewAvailability =
  | PluginProjectionViewAvailable
  | PluginProjectionViewUnavailable;

// Contract-pinned export: tests/authoring/test_plugin_sdk_contract_parity.py scrapes
// this by its exported form — do not demote to module-private. Same
// defaults-plus-declared-only composition as pluginViewContext.ts's
// DEFAULT_PLUGIN_VIEW_CAPABILITIES — see contracts.py CAPABILITY_DEFAULTS /
// CAPABILITY_DECLARED_ONLY.
export const DEFAULT_PLUGIN_PROJECTION_VIEW_CAPABILITIES: PluginProjectionViewCapability[] = [
  'projection.status',
  'projection.build',
  'projection.artifact.read',
  'projection.data.read',
  'host.navigation.openRow',
  'grid.state.read',
  // Projection views wire the same grid-filter fragment plain views get —
  // the geo map's "filter to this area" applies the canonical
  // {geo_col: {bbox}} filter.
  'grid.filter.applyBbox',
  'action.run',
  'host.library.deckgl',
];

declare global {
  interface Window {
    __FRISKET_DISABLED_PLUGIN_PROJECTION_VIEW_CAPABILITIES__?: string[];
    __FRISKET_PLUGIN_PROJECTION_VIEW_TEST_SPIES__?: {
      status?: (args: { contributionId: string; projectionKind: string }) => void;
      build?: (args: {
        contributionId: string;
        projectionKind: string;
        mode: RuntimeProjectionBuildMode;
      }) => void;
      artifactRead?: (args: {
        contributionId: string;
        projectionKind: string;
        artifactId: string;
      }) => void;
      openRow?: (args: { contributionId: string; rowId: string }) => void;
      fetchData?: (args: {
        contributionId: string;
        projectionKind: string;
        columnId: string;
      }) => void;
    };
  }
}

function availablePluginProjectionViewCapabilities(): Set<string> {
  const disabled =
    typeof window === 'undefined'
      ? []
      : window.__FRISKET_DISABLED_PLUGIN_PROJECTION_VIEW_CAPABILITIES__ ?? [];
  const disabledSet = new Set(disabled);
  return new Set(
    DEFAULT_PLUGIN_PROJECTION_VIEW_CAPABILITIES.filter(
      (capability) => !disabledSet.has(capability),
    ),
  );
}

export function resolvePluginProjectionViewAvailability(
  descriptor: WorkbenchViewDescriptor,
  sheet: SheetMeta | null,
  capabilities: Set<string> = availablePluginProjectionViewCapabilities(),
): PluginProjectionViewAvailability {
  for (const requirement of descriptor.requires) {
    if (requirement.kind !== 'hostCapability' || requirement.optional) continue;
    const capabilityId = requirement.id ?? '';
    if (!capabilities.has(capabilityId)) {
      return { available: false, reason: `missing_capability:${capabilityId}` };
    }
  }
  const unmet = firstMissingDataRequirement(
    descriptor.dataRequirements,
    sheetDataRequirementContext(sheet),
  );
  if (unmet) {
    return { available: false, reason: dataRequirementReason(unmet) };
  }
  return { available: true };
}

function columnName(column: ColumnDef): string {
  return column.name.trim().toLowerCase();
}

function findTimelineTarget(sheet: SheetMeta): RuntimeProjectionTarget | null {
  const dateColumn = sheet.columns.find((column) => column.type === 'date');
  if (!dateColumn) return null;
  const titleColumn =
    sheet.columns.find((column) =>
      ['title', 'headline', 'name', 'summary'].includes(columnName(column)),
    ) ??
    sheet.columns.find((column) => column.type === 'text' && column.id !== dateColumn.id) ??
    sheet.columns.find((column) => column.id !== dateColumn.id) ??
    dateColumn;
  const caseColumn = sheet.columns.find((column) => {
    const name = columnName(column);
    return column.id !== dateColumn.id && column.id !== titleColumn.id && /case|matter|id/.test(name);
  });
  return {
    sheetId: sheet.id,
    dateColumnId: dateColumn.id,
    titleColumnId: titleColumn.id,
    ...(caseColumn ? { caseColumnId: caseColumn.id } : {}),
  };
}

export function buildPluginProjectionViewContext({
  descriptor,
  projectId,
  sheet,
  projectionApi,
  openRow,
  hostContext,
  targetColumnId,
  onCloseView,
}: {
  descriptor: WorkbenchViewDescriptor;
  projectId: string;
  sheet: SheetMeta;
  projectionApi: PluginProjectionViewApi;
  openRow(rowId: string): void;
  hostContext: WorkbenchHostContext;
  /** Host-routed column scoping (e.g. "open the map for THIS geo column"):
   *  merged into projection.params as `targetColumnId`. Views opened without
   *  a column route pick their own column from ctx.sheet.columns. */
  targetColumnId?: string;
  /** Host-provided dismissal for host-routed views — becomes
   *  ctx.navigation.closeView. */
  onCloseView?(): void;
}): PluginProjectionViewContext | null {
  const projectionKind = descriptor.projectionKind;
  if (!projectionKind) return null;
  // Timeline-shaped targets when the sheet supports them (dateColumnId is
  // what the artifact_timeline backend path consumes); otherwise a bare
  // sheet target — availability gating belongs to the descriptor's own
  // dataRequirements, not to date-column presence (a geo map view mounts on
  // sheets with no date column at all).
  const target = findTimelineTarget(sheet) ?? { sheetId: sheet.id };
  const params: Record<string, unknown> = {
    ...(descriptor.projectionParams ?? {}),
    ...(targetColumnId !== undefined ? { targetColumnId } : {}),
  };
  return {
    schemaVersion: 'frisket.plugin_projection_view_context.v1',
    projectId,
    contributionId: descriptor.id,
    placement: {
      host: 'mainView',
      mode: 'pane',
    },
    sheet: buildSheetSnapshotFragment(sheet),
    selection: buildSelectionFragment(hostContext),
    ...optionalGridReadSection(descriptor, hostContext),
    ...optionalGridFilterApplySection(descriptor, hostContext),
    ...optionalActionLaunchSection(descriptor, hostContext),
    ...optionalHostLibrarySection(descriptor),
    projection: {
      kind: projectionKind,
      target,
      params,
      status: () => {
        window.__FRISKET_PLUGIN_PROJECTION_VIEW_TEST_SPIES__?.status?.({
          contributionId: descriptor.id,
          projectionKind,
        });
        return projectionApi.getRuntimeProjectionStatus({ projectionKind, target, params });
      },
      build: ({ mode = 'refresh' } = {}) => {
        window.__FRISKET_PLUGIN_PROJECTION_VIEW_TEST_SPIES__?.build?.({
          contributionId: descriptor.id,
          projectionKind,
          mode,
        });
        return projectionApi.buildRuntimeProjection({ projectionKind, target, params, mode });
      },
      readArtifact: (ref) => {
        window.__FRISKET_PLUGIN_PROJECTION_VIEW_TEST_SPIES__?.artifactRead?.({
          contributionId: descriptor.id,
          projectionKind,
          artifactId: ref.artifactId,
        });
        return projectionApi.readRuntimeProjectionArtifact({
          projectionKind,
          artifactId: ref.artifactId,
          target,
          params,
        });
      },
      ...(descriptorDeclaresCapability(descriptor, 'projection.data.read')
        ? {
            fetchData: (dataParams: ProjectionDataReadParams) => {
              window.__FRISKET_PLUGIN_PROJECTION_VIEW_TEST_SPIES__?.fetchData?.({
                contributionId: descriptor.id,
                projectionKind,
                columnId: dataParams.columnId,
              });
              // The SAME map-points route/service the first-party MapView
              // reads through (server/services/map_points.py); filter/sort
              // ride the workspace's real grid state, not a plugin-assembled
              // GridFilterSpec.
              return projectionApi.getMapPointsArrowBuffer(sheet.id, dataParams.columnId, {
                bbox: dataParams.bbox ?? null,
                attrs: dataParams.attrs,
                filter: hostContext.gridState.filter,
                sort: hostContext.gridState.sort,
              });
            },
          }
        : {}),
    },
    navigation: {
      openRow: (rowId) => {
        window.__FRISKET_PLUGIN_PROJECTION_VIEW_TEST_SPIES__?.openRow?.({
          contributionId: descriptor.id,
          rowId,
        });
        openRow(rowId);
      },
      ...(onCloseView ? { closeView: onCloseView } : {}),
    },
  };
}
