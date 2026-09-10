// This checks the FIRST_PARTY_COMMANDS glob conversion. It proves two things
// core/commands/registry.test.ts's pre-existing
// pins do not:
//   1. The discovery path is REAL — FIRST_PARTY_COMMANDS reflects whatever
//      *.command.ts files exist under core/commands/first-party/ on disk right
//      now (read via fs, independent of any hardcoded list in this file), not a
//      name checked into source.
//   2. Every WorkspaceCommand union member still has EXACTLY ONE registered
//      handler, against the REAL glob-built table (registry.test.ts's
//      equivalent assertion also holds; this is the companion pin that runs
//      even when only `core/registration` is filtered).

import { readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { FIRST_PARTY_COMMANDS } from '../commands/registry';
import type { WorkspaceCommand } from '../commands/types';

const firstPartyDir = join(
  dirname(fileURLToPath(import.meta.url)),
  '..',
  'commands',
  'first-party',
);

// The full WorkspaceCommand union membership, duplicated as data — same
// convention as registry.test.ts's EXPECTED_COMMAND_TYPES: the union is the
// type-level inventory (glob kills the VALUE-level duplication), so a
// runtime list against it cannot be eliminated, only its collision risk.
const EXPECTED_COMMAND_TYPES: ReadonlyArray<WorkspaceCommand['type']> = [
  'openSheet',
  'openRow',
  'openColumn',
  'openSource',
  'openEvidence',
  'openMap',
  'openGraph',
  'openActionRoute',
  'openMainViewContribution',
  'import.open',
  'sources.open',
  'export.open',
  'ocrCompare.open',
  'transcribeCompare.open',
  'translateCompare.open',
  'topicCompare.open',
  'settings.open',
];

describe('FIRST_PARTY_COMMANDS — glob self-registration', () => {
  it('registers exactly one file per *.command.ts under core/commands/first-party/', () => {
    const files = readdirSync(firstPartyDir).filter((name) => name.endsWith('.command.ts'));
    expect(files.length).toBeGreaterThan(0);
    // The registry's size is DRIVEN BY the file count on disk — this is what
    // "a new capability is a new file" means: adding/removing a
    // *.command.ts file changes Object.keys(FIRST_PARTY_COMMANDS) with no
    // other code edit.
    expect(Object.keys(FIRST_PARTY_COMMANDS)).toHaveLength(files.length);
  });

  it('every WorkspaceCommand union member has exactly one registered handler', () => {
    const registeredTypes = Object.keys(FIRST_PARTY_COMMANDS).sort();
    expect(registeredTypes).toEqual([...EXPECTED_COMMAND_TYPES].sort());
  });

  it("every descriptor's match equals its table key (no misfiled command file)", () => {
    for (const [key, descriptor] of Object.entries(FIRST_PARTY_COMMANDS)) {
      expect(descriptor.match).toBe(key);
    }
  });

  it('every descriptor has a real run() and testId (glob discovery did not yield a stub)', () => {
    for (const descriptor of Object.values(FIRST_PARTY_COMMANDS)) {
      expect(typeof descriptor.run).toBe('function');
      expect(typeof descriptor.testId).toBe('string');
      expect(descriptor.testId.length).toBeGreaterThan(0);
    }
  });
});
