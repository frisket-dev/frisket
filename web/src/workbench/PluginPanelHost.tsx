import './PluginContributionHosts.css';
import { createElement, useCallback } from 'react';
import type { SheetMeta } from '../api/types';
import { WorkbenchContributionFrame } from './contributions';
import type { WorkbenchPanelDescriptor } from './descriptors';
import type { WorkbenchHostContext } from './hostContext';
import {
  buildPluginPanelContext,
  type PluginPanelContextHost,
} from './pluginPanelContext';
import { nativePanelComponentForKey } from './firstPartyComponents';
import { PluginPanelFamilyHost } from './PluginPanelFamilyHost';

interface PluginPanelHostProps {
  descriptor: WorkbenchPanelDescriptor;
  sheet: SheetMeta | null;
  hostContext: WorkbenchHostContext;
  host?: PluginPanelContextHost;
}

export function PluginPanelHost({ descriptor, sheet, hostContext, host = 'rightInspector' }: PluginPanelHostProps) {
  const buildContext = useCallback(
    (resolvedSheet: SheetMeta) => buildPluginPanelContext({ descriptor, sheet: resolvedSheet, hostContext, host }),
    [descriptor, host, hostContext],
  );
  return (
    <PluginPanelFamilyHost
      family="panel"
      descriptor={descriptor}
      sheet={sheet}
      hostContext={hostContext}
      host={host}
      className="workbench-contribution-host plugin-panel-contribution-host"
      unavailableClassName="plugin-panel-unavailable"
      hidesReason={(reason) => reason.startsWith('data_requirement_unmet:')}
      buildContext={buildContext}
      mountedAttributes={(ctx) => ({ 'data-plugin-panel-selected-count': String(ctx.selection.selectedCount) })}
      renderWithoutRuntime={(ctx, mounted) => {
        // Descriptors without a served moduleUrl fall back to a natively-bundled
        // component looked up by componentKey (firstPartyComponents.ts).
        const nativePanelComponent = nativePanelComponentForKey(descriptor.componentKey);
        if (!nativePanelComponent) return null;
        return (
          <WorkbenchContributionFrame
            descriptor={descriptor}
            host={host}
            className="workbench-contribution-host plugin-panel-contribution-host"
            dataAttributes={mounted}
          >
            {createElement(nativePanelComponent, { ctx })}
          </WorkbenchContributionFrame>
        );
      }}
    />
  );
}
