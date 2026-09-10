import type { CellValue, ColumnDef, Row, SheetMeta } from '../api/types';
import { resolveMediaValue, type ResolvedMediaValue } from '../media/resolveMediaValue';
import {
  dataRequirementReason,
  firstMissingDataRequirement,
  sheetDataRequirementContext,
} from './dataRequirements';
import type { WorkbenchPlacementMode, WorkbenchViewDescriptor } from './descriptors';
import type {
  ActionLaunchFragment,
  GridFilterApplyFragment,
  GridReadFragment,
  HostLibraryDeckglFragment,
  SelectionFragment,
} from './pluginContextFragments';

export type PluginViewCapability =
  | 'sheet.rows.read'
  | 'media.blob.resolve'
  | 'host.navigation.openRow'
  | 'grid.state.read'
  | 'grid.filter.applyBbox'
  | 'action.run'
  | 'host.library.deckgl';

export interface PluginViewContext {
  schemaVersion: 'frisket.plugin_view_context.v1';
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
  /** The workspace's current row selection for the active sheet — the same
   *  live state panel-family contexts see. Persists while this view
   *  occupies mainView; `activeRowId` is the open row drawer, not the
   *  checkbox selection. Always present (not capability-gated) — reuses
   *  the panel selection fragment. */
  selection: SelectionFragment;
  rows: {
    query(args: {
      columnIds?: string[];
      offset: number;
      limit: number;
    }): Promise<{
      rows: Row[];
      total: number;
    }>;
  };
  media: {
    fromCell(value: unknown): ResolvedMediaValue | null;
  };
  navigation: {
    openRow(rowId: string): void;
  };
  /** Present IFF the descriptor declares hostCapability grid.state.read. */
  gridState?: GridReadFragment;
  /** Present IFF the descriptor declares hostCapability
   *  grid.filter.applyBbox (declared by the bundled geo plugin's map view) —
   *  `applyBbox(columnId, bbox)` applies the canonical geo-bbox grid filter
   *  through the host's real "filter to this area" dispatch. */
  gridFilter?: GridFilterApplyFragment;
  /** Present IFF the descriptor declares hostCapability action.run. */
  actions?: ActionLaunchFragment;
  /** Present IFF the descriptor declares hostCapability host.library.deckgl
   *  — libs.deckgl is a Promise for the host-injected deck.gl namespace
   *  (Deck, WebMercatorViewport, ScatterplotLayer, BitmapLayer, TileLayer,
   *  HeatmapLayer), lazily loaded from the SAME chunk the first-party map
   *  ships (web/src/components/map/deckglNamespace.ts) — never a second
   *  copy. See pluginContextFragments.ts optionalHostLibrarySection. */
  libs?: HostLibraryDeckglFragment;
}

export interface PluginViewUnavailable {
  available: false;
  reason: string;
}

export interface PluginViewAvailable {
  available: true;
  reason?: undefined;
}

export type PluginViewAvailability = PluginViewAvailable | PluginViewUnavailable;

// Contract-pinned export: tests/authoring/test_plugin_sdk_contract_parity.py scrapes
// this by its exported form — do not demote to module-private. This is the
// host's full AVAILABLE set, not just the SDK auto-default set: it equals
// contracts.py CAPABILITY_DEFAULTS['view'] plus
// CAPABILITY_DECLARED_ONLY['view'] (host.library.deckgl — available at the
// host but never auto-defaulted; a plugin author must explicitly declare it;
// see contracts.py for the split and its rationale).
const DEFAULT_PLUGIN_VIEW_CAPABILITIES: PluginViewCapability[] = [
  'sheet.rows.read',
  'media.blob.resolve',
  'host.navigation.openRow',
  'grid.state.read',
  'grid.filter.applyBbox',
  'action.run',
  'host.library.deckgl',
];

declare global {
  interface Window {
    __FRISKET_DISABLED_PLUGIN_VIEW_CAPABILITIES__?: string[];
    __FRISKET_PLUGIN_VIEW_TEST_SPIES__?: {
      rowsQuery?: (args: { contributionId: string; offset: number; limit: number }) => void;
      mediaFromCell?: (args: { contributionId: string; hasValue: boolean }) => void;
      openRow?: (args: { contributionId: string; rowId: string }) => void;
      applyBboxFilter?: (args: {
        contributionId: string;
        columnId: string;
        bbox: [number, number, number, number];
      }) => void;
    };
  }
}

export function availablePluginViewCapabilities(): Set<string> {
  const disabled =
    typeof window === 'undefined'
      ? []
      : window.__FRISKET_DISABLED_PLUGIN_VIEW_CAPABILITIES__ ?? [];
  const disabledSet = new Set(disabled);
  return new Set(DEFAULT_PLUGIN_VIEW_CAPABILITIES.filter((capability) => !disabledSet.has(capability)));
}

export function resolvePluginViewAvailability(
  descriptor: WorkbenchViewDescriptor,
  sheet: SheetMeta | null,
  capabilities: Set<string> = availablePluginViewCapabilities(),
): PluginViewAvailability {
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

export function mediaFromPluginViewCell(value: unknown, projectId: string): ResolvedMediaValue | null {
  if (
    value === null ||
    typeof value === 'string' ||
    typeof value === 'number' ||
    typeof value === 'boolean'
  ) {
    return resolveMediaValue(value as CellValue, projectId);
  }
  return resolveMediaValue(JSON.stringify(value), projectId);
}
