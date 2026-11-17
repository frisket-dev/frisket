import '../components/PanelPrimitives.css';
import './contributions.css';
import { useCallback } from 'react';
import type { SheetMeta } from '../api/types';
import type { WorkbenchPanelDescriptor } from './descriptors';
import type { WorkbenchHostContext } from './hostContext';
import { buildPluginDockTabContext } from './pluginDockTabContext';
import { PluginPanelFamilyHost } from './PluginPanelFamilyHost';

interface PluginDockTabHostProps {
  descriptor: WorkbenchPanelDescriptor;
  sheet: SheetMeta | null;
  hostContext: WorkbenchHostContext;
  isActiveTab: boolean;
  focusTab(): void;
}

export function PluginDockTabHost({ descriptor, sheet, hostContext, isActiveTab, focusTab }: PluginDockTabHostProps) {
  const buildContext = useCallback(
    (resolvedSheet: SheetMeta) =>
      buildPluginDockTabContext({ descriptor, sheet: resolvedSheet, hostContext, isActiveTab, focusTab }),
    [descriptor, focusTab, hostContext, isActiveTab],
  );
  return (
    <PluginPanelFamilyHost
      family="dock-tab"
      descriptor={descriptor}
      sheet={sheet}
      hostContext={hostContext}
      host="bottomDock"
      className="bottom-dock-tab-body plugin-dock-tab-contribution-host"
      unavailableClassName="bottom-dock-empty"
      buildContext={buildContext}
      mountedAttributes={(ctx) => ({ 'data-plugin-dock-tab-active': String(ctx.dock.isActiveTab) })}
    />
  );
}
