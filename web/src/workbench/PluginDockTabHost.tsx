import { useMemo } from 'react';
import type { SheetMeta } from '../api/types';
import { WorkbenchContributionFrame } from './contributions';
import type { WorkbenchPanelDescriptor } from './descriptors';
import type { WorkbenchHostContext } from './hostContext';
import { buildPluginDockTabContext } from './pluginDockTabContext';
import { PluginHostShell } from './pluginHostShell';
import { resolvePluginPanelAvailability } from './pluginPanelContext';
import { TrustedLocalPluginComponent } from './TrustedLocalPluginComponent';

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
      <WorkbenchContributionFrame
        descriptor={descriptor}
        host="bottomDock"
        className="bottom-dock-tab-body plugin-dock-tab-contribution-host"
        dataAttributes={{
          'data-plugin-dock-tab-status': 'mounted',
          'data-plugin-dock-tab-context-schema-version': ctx.schemaVersion,
          'data-plugin-dock-tab-active': String(ctx.dock.isActiveTab),
          'data-plugin-runtime-module-url': descriptor.runtimeComponent.moduleUrl,
          'data-plugin-runtime-package-sha256': descriptor.runtimeComponent.packageSha256,
        }}
      >
        <TrustedLocalPluginComponent
          moduleUrl={descriptor.runtimeComponent.moduleUrl}
          componentKey={descriptor.runtimeComponent.componentKey}
          contributionId={descriptor.runtimeComponent.contributionId}
          pluginId={descriptor.runtimeComponent.pluginId}
          ctx={ctx}
        />
      </WorkbenchContributionFrame>
    );
  }

  return unavailableDockTab(descriptor, 'missing_component');
}
