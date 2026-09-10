import type { SheetMeta } from '../api/types';
import type { WorkbenchPanelDescriptor } from './descriptors';
import type { WorkbenchHostContext } from './hostContext';
import {
  buildPanelShapedSections,
  optionalActionLaunchSection,
  optionalGridReadSection,
  type PanelShapedSections,
} from './pluginContextFragments';

// The peek host context: the ONLY peek-side control is close(); backdrop,
// Escape, stacking, and unmount semantics belong to the host.
export interface PluginPeekContext extends PanelShapedSections {
  schemaVersion: 'frisket.plugin_peek_context.v1';
  peek: {
    close(): void;
  };
}

export function buildPluginPeekContext({
  descriptor,
  sheet,
  hostContext,
  close,
}: {
  descriptor: WorkbenchPanelDescriptor;
  sheet: SheetMeta;
  hostContext: WorkbenchHostContext;
  close(): void;
}): PluginPeekContext {
  return {
    schemaVersion: 'frisket.plugin_peek_context.v1',
    ...buildPanelShapedSections({ descriptor, sheet, hostContext }),
    ...optionalGridReadSection(descriptor, hostContext),
    ...optionalActionLaunchSection(descriptor, hostContext),
    peek: {
      close,
    },
  };
}
