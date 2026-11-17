import { useCallback } from 'react';
import type { SheetMeta } from '../api/types';
import type { WorkbenchHostId, WorkbenchPanelDescriptor } from './descriptors';
import type { WorkbenchHostContext } from './hostContext';
import {
  buildPluginDetailContext,
  type PluginDetailSubject,
} from './pluginDetailContext';
import { PluginPanelFamilyHost } from './PluginPanelFamilyHost';

interface PluginDetailHostProps {
  descriptor: WorkbenchPanelDescriptor;
  sheet: SheetMeta | null;
  hostContext: WorkbenchHostContext;
  host: WorkbenchHostId;
  subject: PluginDetailSubject;
}

export function PluginDetailHost({ descriptor, sheet, hostContext, host, subject }: PluginDetailHostProps) {
  const buildContext = useCallback(
    (resolvedSheet: SheetMeta) => buildPluginDetailContext({ descriptor, sheet: resolvedSheet, hostContext, subject }),
    [descriptor, hostContext, subject],
  );
  return (
    <PluginPanelFamilyHost
      family="detail"
      descriptor={descriptor}
      sheet={sheet}
      hostContext={hostContext}
      host={host}
      className="workbench-contribution-host plugin-detail-contribution-host"
      unavailableClassName="plugin-detail-unavailable"
      unavailableAttributes={{ 'data-plugin-detail-subject-kind': subject.kind }}
      buildContext={buildContext}
      mountedAttributes={(ctx) => ({ 'data-plugin-detail-subject-kind': ctx.detail.subject.kind })}
    />
  );
}
