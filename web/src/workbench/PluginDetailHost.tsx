import { useMemo } from 'react';
import type { SheetMeta } from '../api/types';
import { WorkbenchContributionFrame } from './contributions';
import type { WorkbenchHostId, WorkbenchPanelDescriptor } from './descriptors';
import type { WorkbenchHostContext } from './hostContext';
import { PluginHostShell } from './pluginHostShell';
import {
  buildPluginDetailContext,
  type PluginDetailSubject,
} from './pluginDetailContext';
import { resolvePluginPanelAvailability } from './pluginPanelContext';
import { TrustedLocalPluginComponent } from './TrustedLocalPluginComponent';

export interface PluginDetailHostProps {
  descriptor: WorkbenchPanelDescriptor;
  sheet: SheetMeta | null;
  hostContext: WorkbenchHostContext;
  host: WorkbenchHostId;
  subject: PluginDetailSubject;
}

function testIdSuffix(contributionId: string): string {
  return contributionId.replace(/[^a-zA-Z0-9]+/g, '-');
}

function unavailableDetail(
  descriptor: WorkbenchPanelDescriptor,
  host: WorkbenchHostId,
  subject: PluginDetailSubject,
  reason: string,
) {
  return (
    <WorkbenchContributionFrame
      descriptor={descriptor}
      host={host}
      className="workbench-contribution-host plugin-detail-contribution-host"
      dataAttributes={{
        'data-plugin-detail-status': 'unavailable',
        'data-plugin-detail-subject-kind': subject.kind,
        'data-plugin-detail-unavailable-reason': reason,
      }}
    >
      <div
        className="plugin-detail-unavailable"
        data-testid={`plugin-detail-unavailable-${testIdSuffix(descriptor.id)}`}
        data-contribution-id={descriptor.id}
        data-reason={reason}
      >
        {reason}
      </div>
    </WorkbenchContributionFrame>
  );
}

export function PluginDetailHost(props: PluginDetailHostProps) {
  const { descriptor, sheet, hostContext, host, subject } = props;
  return (
    <PluginHostShell
      descriptor={descriptor}
      sheet={sheet}
      resolveAvailability={(resolvedDescriptor, resolvedSheet) =>
        resolvePluginPanelAvailability(resolvedDescriptor, resolvedSheet, hostContext)
      }
      renderUnavailable={(reason) => unavailableDetail(descriptor, host, subject, reason)}
      mount={(resolvedSheet) => (
        <MountedPluginDetailHost {...props} sheet={resolvedSheet} />
      )}
    />
  );
}

function MountedPluginDetailHost({
  descriptor,
  sheet,
  hostContext,
  host,
  subject,
}: {
  descriptor: WorkbenchPanelDescriptor;
  sheet: SheetMeta;
  hostContext: WorkbenchHostContext;
  host: WorkbenchHostId;
  subject: PluginDetailSubject;
}) {
  const ctx = useMemo(
    () => buildPluginDetailContext({ descriptor, sheet, hostContext, subject }),
    [descriptor, hostContext, sheet, subject],
  );

  if (descriptor.runtimeComponent) {
    return (
      <WorkbenchContributionFrame
        descriptor={descriptor}
        host={host}
        className="workbench-contribution-host plugin-detail-contribution-host"
        dataAttributes={{
          'data-plugin-detail-status': 'mounted',
          'data-plugin-detail-context-schema-version': ctx.schemaVersion,
          'data-plugin-detail-subject-kind': ctx.detail.subject.kind,
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

  return unavailableDetail(descriptor, host, subject, 'missing_component');
}
