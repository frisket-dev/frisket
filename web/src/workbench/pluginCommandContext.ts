import type { ColumnDef, SheetMeta } from '../api/types';
import type { WorkbenchCommandDescriptor } from './descriptors';
import type { WorkbenchHostContext } from './hostContext';
import {
  buildSelectionFragment,
  buildSheetSnapshotFragment,
  optionalActionLaunchSection,
  optionalGridReadSection,
  type ActionLaunchFragment,
  type GridReadFragment,
} from './pluginContextFragments';

// The command host context: an INVOCATION-SCOPED snapshot built fresh for each
// palette click — never reactive, never rebuilt while a handler runs. sheet and
// navigation are null when no sheet is active (no no-op stubs).
export interface PluginCommandContext {
  schemaVersion: 'frisket.plugin_command_context.v1';
  projectId: string;
  contributionId: string;
  commandId: string;
  sheet: {
    id: string;
    name: string;
    rowCount: number;
    columns: ColumnDef[];
  } | null;
  selection: {
    selectedRowIds: string[];
    selectedCount: number;
    activeRowId: string | null;
  };
  navigation: {
    openRow(rowId: string): void;
  } | null;
  // Opens one of the INVOKING plugin's own modalOrPeek:peek contributions
  // through the host. Null when the host provides no peek opener. The host
  // rejects foreign or non-peek contribution ids.
  peek: {
    open(contributionId: string): void;
  } | null;
  /** Present IFF the descriptor declares hostCapability grid.state.read
   *  (invocation-scoped snapshot like every other section). */
  gridState?: GridReadFragment;
  /** Present IFF the descriptor declares hostCapability action.run. */
  actions?: ActionLaunchFragment;
}

export function buildPluginCommandContext({
  descriptor,
  sheet,
  hostContext,
  openPeek,
}: {
  descriptor: WorkbenchCommandDescriptor;
  sheet: SheetMeta | null;
  hostContext: WorkbenchHostContext;
  openPeek?: (contributionId: string) => void;
}): PluginCommandContext {
  return {
    schemaVersion: 'frisket.plugin_command_context.v1',
    projectId: hostContext.identity.projectId,
    contributionId: descriptor.id,
    commandId: descriptor.commandId,
    sheet: sheet ? buildSheetSnapshotFragment(sheet) : null,
    selection: buildSelectionFragment(hostContext),
    navigation: sheet
      ? {
          openRow: (rowId) => {
            hostContext.navigation.openRow(sheet.id, rowId);
          },
        }
      : null,
    peek: openPeek
      ? {
          open: (contributionId) => {
            openPeek(contributionId);
          },
        }
      : null,
    ...optionalGridReadSection(descriptor, hostContext),
    ...optionalActionLaunchSection(descriptor, hostContext),
  };
}
