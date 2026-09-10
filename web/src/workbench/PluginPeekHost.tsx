import { useMemo } from 'react';
import type { SheetMeta } from '../api/types';
import { useEscapeDismiss } from '../hooks/useEscapeDismiss';
import { WorkbenchContributionFrame } from './contributions';
import type { WorkbenchPanelDescriptor } from './descriptors';
import type { WorkbenchHostContext } from './hostContext';
import { PluginHostShell } from './pluginHostShell';
import { buildPluginPeekContext } from './pluginPeekContext';
import { resolvePluginPanelAvailability } from './pluginPanelContext';
import { TrustedLocalPluginComponent } from './TrustedLocalPluginComponent';

export interface PluginPeekHostProps {
  descriptor: WorkbenchPanelDescriptor;
  sheet: SheetMeta | null;
  hostContext: WorkbenchHostContext;
  /** Host-owned dismissal: unmounts the peek (the parent conditionally
   *  renders this host — there is no hidden retained subtree). */
  onDismiss(): void;
}

function unavailablePeek(descriptor: WorkbenchPanelDescriptor, reason: string) {
  return (
    <WorkbenchContributionFrame
      descriptor={descriptor}
      host="modalOrPeek"
      className="workbench-contribution-host plugin-peek-contribution-host"
      dataAttributes={{
        'data-plugin-peek-status': 'unavailable',
        'data-plugin-peek-unavailable-reason': reason,
      }}
    >
      <div className="plugin-detail-unavailable" data-testid="plugin-peek-unavailable">
        {reason}
      </div>
    </WorkbenchContributionFrame>
  );
}

// One peek at a time; Escape and backdrop are owned here, never by plugin
// code; dismissal unmounts.
export function PluginPeekHost(props: PluginPeekHostProps) {
  const { descriptor, sheet, hostContext, onDismiss } = props;
  // Host-owned Escape unmounts the peek (never plugin code). The backdrop div
  // below keeps its own click-to-dismiss. The shell is a non-modal `<dialog>`
  // rather than `showModal()` so that clickable backdrop contract survives (a
  // modal dialog's inert background would swallow the pinned backdrop click).
  useEscapeDismiss(onDismiss);

  return (
    <div
      className="workbench-command-palette-backdrop"
      data-testid="plugin-peek-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onDismiss();
      }}
    >
      <dialog
        open
        className="workbench-command-palette plugin-peek-shell"
        data-testid="plugin-peek-shell"
        aria-modal="true"
        aria-label={descriptor.title}
        style={{ position: 'static', margin: 0 }}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <PluginHostShell
          descriptor={descriptor}
          sheet={sheet}
          resolveAvailability={(resolvedDescriptor, resolvedSheet) =>
            resolvePluginPanelAvailability(resolvedDescriptor, resolvedSheet, hostContext)
          }
          renderUnavailable={(reason) => unavailablePeek(descriptor, reason)}
          mount={(resolvedSheet) => (
            <MountedPluginPeek {...props} sheet={resolvedSheet} />
          )}
        />
      </dialog>
    </div>
  );
}

function MountedPluginPeek({
  descriptor,
  sheet,
  hostContext,
  onDismiss,
}: {
  descriptor: WorkbenchPanelDescriptor;
  sheet: SheetMeta;
  hostContext: WorkbenchHostContext;
  onDismiss(): void;
}) {
  const ctx = useMemo(
    () =>
      buildPluginPeekContext({
        descriptor,
        sheet,
        hostContext,
        close: onDismiss,
      }),
    [descriptor, hostContext, onDismiss, sheet],
  );

  if (!descriptor.runtimeComponent) {
    return (
      <div className="plugin-detail-unavailable" data-testid="plugin-peek-unavailable">
        missing_component
      </div>
    );
  }

  return (
    <WorkbenchContributionFrame
      descriptor={descriptor}
      host="modalOrPeek"
      className="workbench-contribution-host plugin-peek-contribution-host"
      dataAttributes={{
        'data-plugin-peek-status': 'mounted',
        'data-plugin-peek-context-schema-version': ctx.schemaVersion,
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
