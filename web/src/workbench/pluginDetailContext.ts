import type { SheetMeta } from '../api/types';
import type { WorkbenchPanelDescriptor } from './descriptors';
import type { WorkbenchHostContext } from './hostContext';
import {
  buildPanelShapedSections,
  optionalActionLaunchSection,
  optionalGridReadSection,
  type PanelShapedSections,
} from './pluginContextFragments';

// The subject a detail surface is about — discriminated so a plugin written
// for one subject kind fails obviously (not silently) on another.
export type PluginDetailSubject =
  | { kind: 'row'; sheetId: string; rowId: string }
  | { kind: 'column'; sheetId: string; columnId: string; columnName: string }
  | { kind: 'entity'; entityId: string; label: string }
  | { kind: 'source'; sourceId: string };

export interface PluginDetailContext extends PanelShapedSections {
  schemaVersion: 'frisket.plugin_detail_context.v1';
  detail: {
    subject: PluginDetailSubject;
  };
}

export function buildPluginDetailContext({
  descriptor,
  sheet,
  hostContext,
  subject,
}: {
  descriptor: WorkbenchPanelDescriptor;
  sheet: SheetMeta;
  hostContext: WorkbenchHostContext;
  subject: PluginDetailSubject;
}): PluginDetailContext {
  return {
    schemaVersion: 'frisket.plugin_detail_context.v1',
    ...buildPanelShapedSections({ descriptor, sheet, hostContext }),
    ...optionalGridReadSection(descriptor, hostContext),
    ...optionalActionLaunchSection(descriptor, hostContext),
    detail: {
      subject,
    },
  };
}
