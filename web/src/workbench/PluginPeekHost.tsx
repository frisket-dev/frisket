import './WorkbenchCommandPalette.css';
import '../workspace/WorkspaceOverlays.css';
import { useCallback } from 'react';
import type { SheetMeta } from '../api/types';
import { useEscapeDismiss } from '../hooks/useEscapeDismiss';
import type { WorkbenchPanelDescriptor } from './descriptors';
import type { WorkbenchHostContext } from './hostContext';
import { buildPluginPeekContext } from './pluginPeekContext';
import { PluginPanelFamilyHost } from './PluginPanelFamilyHost';

interface PluginPeekHostProps {
  descriptor: WorkbenchPanelDescriptor;
  sheet: SheetMeta | null;
  hostContext: WorkbenchHostContext;
  /** Host-owned dismissal: unmounts the peek (the parent conditionally
   *  renders this host — there is no hidden retained subtree). */
  onDismiss(): void;
}

function peekUnavailableBody(reason: string) {
  return (
    <div className="plugin-detail-unavailable" data-testid="plugin-peek-unavailable">
      {reason}
    </div>
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
  const buildContext = useCallback(
    (resolvedSheet: SheetMeta) =>
      buildPluginPeekContext({ descriptor, sheet: resolvedSheet, hostContext, close: onDismiss }),
    [descriptor, hostContext, onDismiss],
  );

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
        <PluginPanelFamilyHost
          family="peek"
          descriptor={descriptor}
          sheet={sheet}
          hostContext={hostContext}
          host="modalOrPeek"
          className="workbench-contribution-host plugin-peek-contribution-host"
          renderUnavailableBody={peekUnavailableBody}
          buildContext={buildContext}
          renderWithoutRuntime={() => peekUnavailableBody('missing_component')}
        />
      </dialog>
    </div>
  );
}
