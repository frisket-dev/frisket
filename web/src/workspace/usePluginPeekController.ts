import { useCallback, useMemo, useState } from 'react';
import type { WorkbenchPanelDescriptor } from '../workbench/descriptors';

interface PluginPeekState {
  openContributionId: string | null;
  openOwnerPluginId: string | null;
  rejectedContributionId: string | null;
}

const CLOSED_PLUGIN_PEEK_STATE: PluginPeekState = {
  openContributionId: null,
  openOwnerPluginId: null,
  rejectedContributionId: null,
};

/**
 * One plugin peek at a time, opened only through a command's ctx.peek.open
 * (user gesture), unmounted on dismiss.
 * Owner-scoping applies at open time AND resolve time so two plugins with a
 * colliding contribution id never swap surfaces.
 */
export function usePluginPeekController(
  modalOrPeekPluginPanelDescriptors: WorkbenchPanelDescriptor[],
) {
  const [pluginPeekState, setPluginPeekState] = useState<PluginPeekState>(
    CLOSED_PLUGIN_PEEK_STATE,
  );
  const dismissPluginPeek = useCallback(() => {
    setPluginPeekState(CLOSED_PLUGIN_PEEK_STATE);
  }, []);
  const openPluginPeekForPlugin = useCallback(
    (ownerPluginId: string) => (contributionId: string) => {
      const descriptor = modalOrPeekPluginPanelDescriptors.find(
        (candidate) =>
          candidate.id === contributionId && candidate.ownerPluginId === ownerPluginId,
      );
      if (!descriptor) {
        // Foreign or non-peek contribution: rejected, never opened.
        setPluginPeekState((current) => ({
          ...current,
          rejectedContributionId: contributionId,
        }));
        return;
      }
      // Keep any rejection recorded earlier in the same gesture; dismiss
      // clears both.
      setPluginPeekState((current) => ({
        openContributionId: contributionId,
        openOwnerPluginId: ownerPluginId,
        rejectedContributionId: current.rejectedContributionId,
      }));
    },
    [modalOrPeekPluginPanelDescriptors],
  );
  const openPluginPeekDescriptor = useMemo(
    () =>
      pluginPeekState.openContributionId
        ? modalOrPeekPluginPanelDescriptors.find(
            (candidate) =>
              candidate.id === pluginPeekState.openContributionId &&
              // Owner-scoped like the open-time check: two plugins with a
              // colliding contribution id never swap surfaces.
              candidate.ownerPluginId === pluginPeekState.openOwnerPluginId,
          ) ?? null
        : null,
    [
      modalOrPeekPluginPanelDescriptors,
      pluginPeekState.openContributionId,
      pluginPeekState.openOwnerPluginId,
    ],
  );
  return {
    dismissPluginPeek,
    openPluginPeekDescriptor,
    openPluginPeekForPlugin,
    pluginPeekState,
  };
}
