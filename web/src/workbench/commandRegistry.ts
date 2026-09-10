import type { SheetMeta } from '../api/types';
import { dispatch, FIRST_PARTY_COMMANDS } from '../core/commands/registry';
import type { CommandContext, WorkspaceCommand } from '../core/commands/types';
import type { WorkbenchCommandDescriptor } from './descriptors';
import type { WorkbenchHostContext } from './hostContext';
import { buildPluginCommandContext } from './pluginCommandContext';
import { resolvePluginPanelAvailability } from './pluginPanelContext';
import { loadTrustedLocalPluginExport } from './trustedLocalModule';

// A palette entry: one command descriptor plus its runnable binding. Entries
// are built per project render (the runtime index is project-scoped), and a
// handler runs ONLY from an explicit palette click — the palette never
// auto-invokes on open and there are no plugin keybindings.
export interface WorkbenchCommandEntry {
  descriptor: WorkbenchCommandDescriptor;
  run(): void | Promise<void>;
  disabled?: boolean;
  availabilityStatus?: string;
  availabilityReason?: string;
}

type TrustedLocalCommandHandler = (args: { ctx: unknown }) => void | Promise<void>;

export function pluginCommandEntry({
  descriptor,
  sheet,
  hostContext,
  onRan,
  openPeek,
}: {
  descriptor: WorkbenchCommandDescriptor;
  sheet: SheetMeta | null;
  hostContext: WorkbenchHostContext;
  onRan?: (descriptor: WorkbenchCommandDescriptor) => void;
  /** Host-provided peek opener, already scoped to the invoking plugin. */
  openPeek?: (contributionId: string) => void;
}): WorkbenchCommandEntry {
  const availability = resolvePluginPanelAvailability(descriptor, sheet, hostContext);
  return {
    descriptor,
    disabled: !availability.available,
    availabilityStatus: availability.available ? 'enabled' : 'disabled',
    availabilityReason: availability.reason,
    run: async () => {
      const runtimeComponent = descriptor.runtimeComponent;
      if (!runtimeComponent || !availability.available) return;
      // Same integrity path as every plugin frontend export: the served
      // module URL fails closed on package drift.
      const handler = (await loadTrustedLocalPluginExport(
        runtimeComponent.moduleUrl,
        descriptor.handlerKey,
      )) as TrustedLocalCommandHandler;
      const ctx = buildPluginCommandContext({ descriptor, sheet, hostContext, openPeek });
      await handler({ ctx });
      onRan?.(descriptor);
    },
  };
}

// ---- First-party composition seam ------------------------------------
//
// A first-party WorkspaceCommand adapts into the SAME WorkbenchCommandEntry
// shape the palette already consumes, so `workbenchCommandEntries` becomes
// `[...pluginEntries, ...firstPartyEntries]` — one entry type, two builders.
// The plugin path (pluginCommandEntry, above) is UNTOUCHED: its descriptor
// contract, availability, and sandboxed dynamic-import execution are unchanged.
//
// `descriptor` is passed EXPLICITLY (not derived): only the five commands that
// are palette-exposed today (registry.PALETTE_EXPOSED_COMMANDS) supply one.
// Internal-only commands never call this; they are plain dispatch() with no
// WorkbenchCommandDescriptor artifact.
//
// Enablement/run route entirely through the internal registry.
export function firstPartyCommandEntry(
  command: WorkspaceCommand,
  ctx: CommandContext,
  descriptor: WorkbenchCommandDescriptor,
): WorkbenchCommandEntry {
  const enabled = FIRST_PARTY_COMMANDS[command.type].enabled(ctx);
  return {
    descriptor,
    disabled: !enabled,
    availabilityStatus: enabled ? 'enabled' : 'disabled',
    run: () => dispatch(command, ctx),
  };
}
