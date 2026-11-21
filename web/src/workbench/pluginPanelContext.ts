import type { ColumnDef, SheetMeta } from '../api/types';
import {
  resolveRequirementAvailability,
  sheetDataRequirementContext,
  withoutDisabledCapabilities,
  type RequirementAvailability,
  type WorkbenchDataRequirementContext,
} from './dataRequirements';
import type { WorkbenchPanelDescriptor, WorkbenchPlacementMode } from './descriptors';
import {
  hostContextCapabilityAttribute,
  type WorkbenchHostContext,
} from './hostContext';
import {
  buildPanelShapedSections,
  optionalActionLaunchSection,
  optionalGridReadSection,
  type ActionLaunchFragment,
  type GridReadFragment,
} from './pluginContextFragments';

type PluginPanelCapability =
  | 'sheet.active'
  | 'selection.rows'
  | 'host.navigation.openRow'
  | 'grid.state.read'
  | 'action.run';

// placement.host widened additively per host cohort (schemaVersion never
// re-versions for additive placement union growth).
export type PluginPanelContextHost = 'rightInspector' | 'leftSidebar';

export interface PluginPanelContext {
  schemaVersion: 'frisket.plugin_panel_context.v1';
  projectId: string;
  contributionId: string;
  placement: {
    host: PluginPanelContextHost;
    mode: Extract<WorkbenchPlacementMode, 'panel'>;
  };
  sheet: {
    id: string;
    name: string;
    rowCount: number;
    columns: ColumnDef[];
  };
  selection: {
    selectedRowIds: string[];
    selectedCount: number;
    activeRowId: string | null;
  };
  grid: {
    filter: WorkbenchHostContext['gridState']['filter'];
    sort: WorkbenchHostContext['gridState']['sort'];
  };
  navigation: {
    openRow(rowId: string): void;
  };
  /** Present IFF the descriptor declares hostCapability grid.state.read. */
  gridState?: GridReadFragment;
  /** Present IFF the descriptor declares hostCapability action.run. */
  actions?: ActionLaunchFragment;
}

// Contract-pinned export: tests/authoring/test_plugin_sdk_contract_parity.py scrapes
// this by its exported form — do not demote to module-private.
export const DEFAULT_PLUGIN_PANEL_CAPABILITIES: PluginPanelCapability[] = [
  'sheet.active',
  'selection.rows',
  'host.navigation.openRow',
  'grid.state.read',
  'action.run',
];

declare global {
  interface Window {
    __FRISKET_DISABLED_PLUGIN_PANEL_CAPABILITIES__?: string[];
    __FRISKET_PLUGIN_PANEL_TEST_SPIES__?: {
      openRow?: (args: { contributionId: string; rowId: string }) => void;
    };
  }
}

function availablePluginPanelCapabilities(
  hostContext: WorkbenchHostContext,
): Set<string> {
  const hostCapabilities = hostContextCapabilityAttribute(hostContext)
    .split(' ')
    .filter((capability) => DEFAULT_PLUGIN_PANEL_CAPABILITIES.includes(capability as PluginPanelCapability));
  return withoutDisabledCapabilities(
    hostCapabilities,
    (host) => host.__FRISKET_DISABLED_PLUGIN_PANEL_CAPABILITIES__,
  );
}

function pluginPanelDataRequirementContext(
  sheet: SheetMeta | null,
  hostContext: WorkbenchHostContext,
): WorkbenchDataRequirementContext {
  return {
    ...sheetDataRequirementContext(sheet),
    activeRow: hostContext.selection.activeRowId !== null,
    activeColumn: hostContext.selection.activeColumnId !== null,
    activeCell: hostContext.selection.activeCell !== null,
    activeEvidence: hostContext.selection.activeEvidenceLinkId !== null,
    activeFeed: hostContext.selection.activeFeedId !== null,
    activeEntity: hostContext.selection.activeEntityId !== null,
    selectedRowIds: hostContext.selection.selectedRowIds,
  };
}

// Structurally typed so the panel-shaped hosts (right inspector, dock tabs)
// and palette commands share one requires/dataRequirements evaluator.
export function resolvePluginPanelAvailability(
  descriptor: Pick<WorkbenchPanelDescriptor, 'requires' | 'dataRequirements'>,
  sheet: SheetMeta | null,
  hostContext: WorkbenchHostContext,
  capabilities: Set<string> = availablePluginPanelCapabilities(hostContext),
): RequirementAvailability {
  return resolveRequirementAvailability(
    descriptor,
    capabilities,
    pluginPanelDataRequirementContext(sheet, hostContext),
  );
}

export function buildPluginPanelContext({
  descriptor,
  sheet,
  hostContext,
  host = 'rightInspector',
}: {
  descriptor: WorkbenchPanelDescriptor;
  sheet: SheetMeta;
  hostContext: WorkbenchHostContext;
  host?: PluginPanelContextHost;
}): PluginPanelContext {
  return {
    schemaVersion: 'frisket.plugin_panel_context.v1',
    placement: {
      host,
      mode: 'panel',
    },
    ...buildPanelShapedSections({ descriptor, sheet, hostContext }),
    ...optionalGridReadSection(descriptor, hostContext),
    ...optionalActionLaunchSection(descriptor, hostContext),
  };
}
