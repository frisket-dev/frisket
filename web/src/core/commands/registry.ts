// The INTERNAL WorkspaceCommand registry: every member of the WorkspaceCommand
// union (types.ts) gets a plain typed CommandDescriptor here so it can be
// dispatch()-ed. This is NOT the palette artifact — palette exposure is a
// separate concept (WorkbenchCommandDescriptor, descriptors.ts) that only the
// commands in PALETTE_EXPOSED_COMMANDS carry today; every other member is
// internal-only dispatch with no descriptor artifact and no
// firstPartyComponents binding.
//
// Framework-free (core/ boundary).
//
// FIRST_PARTY_COMMANDS is built by `import.meta.glob('./first-party/*.command.ts',
// { eager: true })`: each WorkspaceCommand member lives in its own file under
// first-party/, default-exporting one CommandDescriptor. A new command = a new
// file; two files claiming the same `match` throw at module load
// (core/registration/glob.ts's collectEagerRegistrations) instead of silently
// overwriting. Vite's import.meta.glob is understood natively by vitest 4 (it
// runs its own Vite module-transform pipeline even for `environment: 'node'`
// tests, not just for browser mode) — no separate glob plugin/config is
// needed; core/registration/commandRegistration.test.ts proves the discovery
// path directly against the built module.
//
// enabled() stays `() => true` for every descriptor: every moved body already
// carries its OWN guard as an inline early return (e.g. openMapPanel's
// `if (col.type !== 'geo_point') return;`, `isContributionHidden(...)` checks)
// — duplicating those into enabled() would risk a second, driftable copy of
// the same condition for zero observed behavior gain (none of the
// palette-exposed commands have a guard today, so no disabled-state UI depends
// on it). Deferred, not dropped: a future stage may hoist real gates into
// enabled() once the domain stores that back them exist.

import { collectEagerRegistrations } from '../registration/glob';
import type { CommandDescriptor, CommandContext, WorkspaceCommand } from './types';

export type { CommandDescriptor };

// ---- The internal registry — glob self-registration -----------------------
//
// A per-key MAPPED type (not a plain Record<Type, CommandDescriptor>) is
// still the EXPORTED shape, so downstream indexing (`FIRST_PARTY_COMMANDS[
// 'openSheet']`) narrows the same way it did as a hand-typed literal. TS
// cannot verify completeness of a dynamically-built glob map against the
// WorkspaceCommand union at compile time the way it could verify an object
// LITERAL against a mapped type — that completeness guarantee moves to
// test-time: core/commands/registry.test.ts
// AND core/registration/commandRegistration.test.ts both assert every union
// member has exactly one registered handler, against the REAL glob-built
// table (not a mock). The union type itself (types.ts) remains the single
// TYPE-level inventory; glob kills the VALUE-level duplication risk.

type CommandDescriptorTable = {
  [K in WorkspaceCommand['type']]: CommandDescriptor<Extract<WorkspaceCommand, { type: K }>>;
};

// Eager: every first-party command handler must be registered synchronously
// at module load (dispatch() cannot await a lazy import mid-command), and
// eager evaluation is what makes a duplicate `match` throw AT IMPORT TIME
// rather than lazily on first dispatch of the colliding command.
const firstPartyCommandModules = import.meta.glob('./first-party/*.command.ts', {
  eager: true,
}) as Record<string, { default: CommandDescriptor }>;

export const FIRST_PARTY_COMMANDS = collectEagerRegistrations(
  firstPartyCommandModules,
  (descriptor) => descriptor.match,
  'first-party command',
) as CommandDescriptorTable;

// ---- Internal vs. palette-exposed ------------------------------------------
//
// The WorkspaceCommand members that appear in the ⌘K palette TODAY. Maps each
// to the id of its EXISTING WorkbenchCommandDescriptor (descriptors.ts)
// — the ones that already ship. Every other union member is internal-only: it
// has a CommandDescriptor above but NO WorkbenchCommandDescriptor artifact and
// NO web/src/workbench/firstPartyComponents.ts binding (so
// tests/server/test_first_party_descriptor_package.py is unaffected). A future
// change that newly palette-exposes a command must add its descriptor
// artifact + firstPartyComponents binding separately.
//
// The id strings are just constants (not a workbench/ import) so this stays
// inside the core/ boundary. This small table is NOT glob-converted: it is a
// genuinely small, rarely-changed exposure allowlist (4 of the 15 commands),
// not a duplication-prone per-command inventory.
export const PALETTE_EXPOSED_COMMANDS: Readonly<
  Partial<Record<WorkspaceCommand['type'], string>>
> = {
  'sources.open': 'frisket.core.command.open_sources',
  'settings.open': 'frisket.core.command.open_settings',
  'ocrCompare.open': 'frisket.media.command.ocr_compare',
  'transcribeCompare.open': 'frisket.media.command.transcribe_compare',
  'translateCompare.open': 'frisket.media.command.translate_compare',
  'topicCompare.open': 'frisket.media.command.topic_segmentation_compare',
};

// ---- dispatch ---------------------------------------------------------

/** Enablement is the single gate: a disabled command is a no-op. */
export function dispatch(
  command: WorkspaceCommand,
  ctx: CommandContext,
): void | Promise<void> {
  // The lookup key (command.type) isn't narrowed to a single literal here —
  // command is the FULL union — so indexing the per-key mapped
  // CommandDescriptorTable yields a union of differently-narrowed
  // CommandDescriptor<C> types, none of which tsc can statically match back
  // to `command`'s own (also-unnarrowed) type. The cast is the one honest
  // escape hatch at this boundary: it's safe because FIRST_PARTY_COMMANDS is
  // built key-by-key with each entry's own `match` equal to its table key
  // (enforced both by collectEagerRegistrations's keyOf and by
  // registry.test.ts's runtime assertion), so `command.type`'s descriptor
  // always has a run() that accepts exactly `command`'s shape at runtime.
  const descriptor = FIRST_PARTY_COMMANDS[command.type] as CommandDescriptor<WorkspaceCommand>;
  if (!descriptor.enabled(ctx)) return;
  return descriptor.run(command, ctx);
}
