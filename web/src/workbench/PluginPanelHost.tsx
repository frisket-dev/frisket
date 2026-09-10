import { createElement, useMemo } from 'react';
import type { SheetMeta } from '../api/types';
import { WorkbenchContributionFrame } from './contributions';
import type { WorkbenchPanelDescriptor } from './descriptors';
import type { WorkbenchHostContext } from './hostContext';
import { PluginHostShell } from './pluginHostShell';
import {
  buildPluginPanelContext,
  resolvePluginPanelAvailability,
  type PluginPanelContextHost,
} from './pluginPanelContext';
import { nativePanelComponentForKey } from './firstPartyComponents';
import { TrustedLocalPluginComponent } from './TrustedLocalPluginComponent';

export interface PluginPanelHostProps {
  descriptor: WorkbenchPanelDescriptor;
  sheet: SheetMeta | null;
  hostContext: WorkbenchHostContext;
  host?: PluginPanelContextHost;
}

function testIdSuffix(contributionId: string): string {
  return contributionId.replace(/[^a-zA-Z0-9]+/g, '-');
}

function unavailablePanel(
  descriptor: WorkbenchPanelDescriptor,
  host: PluginPanelContextHost,
  reason: string,
) {
  return (
    <WorkbenchContributionFrame
      descriptor={descriptor}
      host={host}
      className="workbench-contribution-host plugin-panel-contribution-host"
      dataAttributes={{
        'data-plugin-panel-status': 'unavailable',
        'data-plugin-panel-unavailable-reason': reason,
      }}
    >
      <div
        className="plugin-panel-unavailable"
        data-testid={`plugin-panel-unavailable-${testIdSuffix(descriptor.id)}`}
        data-contribution-id={descriptor.id}
        data-reason={reason}
      >
        {reason}
      </div>
    </WorkbenchContributionFrame>
  );
}

export function PluginPanelHost(props: PluginPanelHostProps) {
  const { descriptor, sheet, hostContext, host = 'rightInspector' } = props;
  return (
    <PluginHostShell
      descriptor={descriptor}
      sheet={sheet}
      resolveAvailability={(resolvedDescriptor, resolvedSheet) =>
        resolvePluginPanelAvailability(resolvedDescriptor, resolvedSheet, hostContext)
      }
      renderUnavailable={(reason) =>
        reason.startsWith('data_requirement_unmet:')
          ? null
          : unavailablePanel(descriptor, host, reason)
      }
      mount={(resolvedSheet) => (
        <MountedPluginPanelHost {...props} sheet={resolvedSheet} host={host} />
      )}
    />
  );
}

function MountedPluginPanelHost({
  descriptor,
  sheet,
  hostContext,
  host,
}: {
  descriptor: WorkbenchPanelDescriptor;
  sheet: SheetMeta;
  hostContext: WorkbenchHostContext;
  host: PluginPanelContextHost;
}) {
  const ctx = useMemo(
    () => buildPluginPanelContext({ descriptor, sheet, hostContext, host }),
    [descriptor, host, hostContext, sheet],
  );

  if (descriptor.runtimeComponent) {
    return (
      <WorkbenchContributionFrame
        descriptor={descriptor}
        host={host}
        className="workbench-contribution-host plugin-panel-contribution-host"
        dataAttributes={{
          'data-plugin-panel-status': 'mounted',
          'data-plugin-panel-context-schema-version': ctx.schemaVersion,
          'data-plugin-panel-selected-count': String(ctx.selection.selectedCount),
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

  // Descriptors without a served moduleUrl fall back to a natively-bundled
  // component looked up by componentKey through the shared registry
  // (firstPartyComponents.ts) instead of a hardcoded per-componentKey
  // comparison.
  const nativePanelComponent = nativePanelComponentForKey(descriptor.componentKey);
  if (nativePanelComponent) {
    return (
      <WorkbenchContributionFrame
        descriptor={descriptor}
        host={host}
        className="workbench-contribution-host plugin-panel-contribution-host"
        dataAttributes={{
          'data-plugin-panel-status': 'mounted',
          'data-plugin-panel-context-schema-version': ctx.schemaVersion,
          'data-plugin-panel-selected-count': String(ctx.selection.selectedCount),
        }}
      >
        {createElement(nativePanelComponent, { ctx })}
      </WorkbenchContributionFrame>
    );
  }

  return unavailablePanel(descriptor, host, 'missing_component');
}
