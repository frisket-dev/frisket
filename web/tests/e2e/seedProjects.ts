// The demo projects scripts/e2e/seed_demo.py creates, and the verdict
// seed.setup.ts records about each one so specs that open them fail at once
// with the recorded reason instead of timing out against a missing or
// unopenable project.
import fs from 'node:fs';
import path from 'node:path';

export const SEEDED_PROJECTS = [
  'Local stories',
  'Council audio',
  'Tariff impacts',
  'People mentioned',
] as const;
export type SeededProject = (typeof SEEDED_PROJECTS)[number];

// project name -> null when it opens, else why it cannot be used
export type SeedStatus = Partial<Record<SeededProject, string | null>>;

export const SEED_REMEDY =
  'the setup rebuilds a missing or partial project, without model keys under ' +
  'FRISKET_CACHE_MODE=replay_strict; "Council audio" also needs the pinned Whisper base model ' +
  '(python scripts/e2e/fetch_whisper_model.py). A project from an older schema is rebuilt ' +
  'once it is deleted from $FRISKET_E2E_BASE_WS (default ~/.frisket/e2e-ws)';

function statusPath(): string | null {
  const workspace = process.env.FRISKET_E2E_WS;
  return workspace ? path.join(workspace, '.frisket-e2e-seed-status.json') : null;
}

export function writeSeedStatus(status: SeedStatus): void {
  const file = statusPath();
  if (file) fs.writeFileSync(file, `${JSON.stringify(status, null, 1)}\n`);
}

function readSeedStatus(): SeedStatus | null {
  const file = statusPath();
  if (!file || !fs.existsSync(file)) return null;
  return JSON.parse(fs.readFileSync(file, 'utf8')) as SeedStatus;
}

function isSeededProject(name: string): name is SeededProject {
  return (SEEDED_PROJECTS as readonly string[]).includes(name);
}

/**
 * Throws at once when seed.setup.ts recorded `name` as unusable. A run that
 * skipped the setup (`--no-deps`) has no record and falls through to the
 * caller's own lookup.
 */
export function requireSeededProject(name: string): void {
  if (!isSeededProject(name)) return;
  const reason = readSeedStatus()?.[name];
  if (reason) throw new Error(`seeded project "${name}" is unavailable: ${reason}; ${SEED_REMEDY}`);
}
