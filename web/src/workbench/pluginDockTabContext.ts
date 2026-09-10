import type { SheetMeta } from '../api/types';
import type { WorkbenchPanelDescriptor, WorkbenchPlacementMode } from './descriptors';
import type { WorkbenchHostContext } from './hostContext';
import type { PluginPanelContext } from './pluginPanelContext';
import {
  buildPanelShapedSections,
  optionalActionLaunchSection,
  optionalGridReadSection,
} from './pluginContextFragments';

// The dock-tab host context: the panel-shaped sections plus the dock section.
// Availability (capabilities + dataRequirements) intentionally reuses
// resolvePluginPanelAvailability — dock tabs are panel-shaped surfaces.
export interface PluginDockTabContext
  extends Omit<PluginPanelContext, 'schemaVersion' | 'placement'> {
  schemaVersion: 'frisket.plugin_dock_tab_context.v1';
  placement: {
    host: 'bottomDock';
    mode: Extract<WorkbenchPlacementMode, 'tab'>;
  };
  dock: {
    isActiveTab: boolean;
    // Selects this tab through the host — a user-facing affordance the host
    // owns; never invoked by the host itself (no auto-focus on mount).
    focus(): void;
  };
}

export function buildPluginDockTabContext({
  descriptor,
  sheet,
  hostContext,
  isActiveTab,
  focusTab,
}: {
  descriptor: WorkbenchPanelDescriptor;
  sheet: SheetMeta;
  hostContext: WorkbenchHostContext;
  isActiveTab: boolean;
  focusTab(): void;
}): PluginDockTabContext {
  return {
    schemaVersion: 'frisket.plugin_dock_tab_context.v1',
    placement: {
      host: 'bottomDock',
      mode: 'tab',
    },
    ...buildPanelShapedSections({ descriptor, sheet, hostContext }),
    ...optionalGridReadSection(descriptor, hostContext),
    ...optionalActionLaunchSection(descriptor, hostContext),
    dock: {
      isActiveTab,
      focus: focusTab,
    },
  };
}
