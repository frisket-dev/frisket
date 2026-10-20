import '../components/PanelPrimitives.css';
import './contributions.css';
import { useMemo } from 'react';
import type { SheetMeta } from '../api/types';
import { WorkbenchContributionFrame } from './contributions';
import type { WorkbenchPanelDescriptor } from './descriptors';
import type { WorkbenchHostContext } from './hostContext';
import { buildPluginDockTabContext } from './pluginDockTabContext';
import { PluginHostShell } from './pluginHostShell';
import { resolvePluginPanelAvailability } from './pluginPanelContext';
import { PluginRuntimeContribution } from './PluginRuntimeContribution';

export interface PluginDockTabHostProps {
  descriptor: WorkbenchPanelDescriptor;
  sheet: SheetMeta | null;
  hostContext: WorkbenchHostContext;
  isActiveTab: boolean;
  focusTab(): void;
}

function testIdSuffix(contributionId: string): string {
  return contributionId.replace(/[^a-zA-Z0-9]+/g, '-');
}

function unavailableDockTab(descriptor: WorkbenchPanelDescriptor, reason: string) {
  return (
    <WorkbenchContributionFrame
      descriptor={descriptor}
      host="bottomDock"
      className="bottom-dock-tab-body plugin-dock-tab-contribution-host"
      dataAttributes={{
        'data-plugin-dock-tab-status': 'unavailable',
        'data-plugin-dock-tab-unavailable-reason': reason,
      }}
    >
      <div
        className="bottom-dock-empty"
        data-testid={`plugin-dock-tab-unavailable-${testIdSuffix(descriptor.id)}`}
        data-contribution-id={descriptor.id}
        data-reason={reason}
      >
        {reason}
      </div>
    </WorkbenchContributionFrame>
  );
}

export function PluginDockTabHost(props: PluginDockTabHostProps) {
  const { descriptor, sheet, hostContext } = props;
  return (
    <PluginHostShell
      descriptor={descriptor}
      sheet={sheet}
      resolveAvailability={(resolvedDescriptor, resolvedSheet) =>
        resolvePluginPanelAvailability(resolvedDescriptor, resolvedSheet, hostContext)
      }
      renderUnavailable={(reason) => unavailableDockTab(descriptor, reason)}
      mount={(resolvedSheet) => (
        <MountedPluginDockTabHost {...props} sheet={resolvedSheet} />
      )}
    />
  );
}

function MountedPluginDockTabHost({
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
}) {
  const ctx = useMemo(
    () =>
      buildPluginDockTabContext({
        descriptor,
        sheet,
        hostContext,
        isActiveTab,
        focusTab,
      }),
    [descriptor, focusTab, hostContext, isActiveTab, sheet],
  );

  if (descriptor.runtimeComponent) {
    return (
      <PluginRuntimeContribution
        descriptor={descriptor}
        host="bottomDock"
        className="bottom-dock-tab-body plugin-dock-tab-contribution-host"
        dataAttributes={{
          'data-plugin-dock-tab-status': 'mounted',
          'data-plugin-dock-tab-context-schema-version': ctx.schemaVersion,
          'data-plugin-dock-tab-active': String(ctx.dock.isActiveTab),
        }}
        runtime={descriptor.runtimeComponent}
        ctx={ctx}
      />
    );
  }

  return unavailableDockTab(descriptor, 'missing_component');
}
