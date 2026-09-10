import { spawnSync } from 'node:child_process';
import { delimiter, resolve } from 'node:path';
import { existsSync } from 'node:fs';

import type { ActionCatalogPayload } from '../../src/api/types';

const repositoryRoot = resolve(process.cwd(), '..');
const repositoryPython = resolve(repositoryRoot, '.venv/bin/python');

export function servedActionCatalogPython(): string {
  if (process.env.FRISKET_TEST_PYTHON) return process.env.FRISKET_TEST_PYTHON;
  if (process.env.VIRTUAL_ENV) return resolve(process.env.VIRTUAL_ENV, 'bin/python');
  return repositoryPython;
}

export function hasServedActionCatalogPython(): boolean {
  return existsSync(servedActionCatalogPython());
}

let servedActionCatalogCache: ActionCatalogPayload | null = null;

/**
 * Load normalized backend-served catalog truth once per Vitest worker.
 *
 * Focused UI tests use actionCatalogFixtures.ts instead. This slower boundary
 * is reserved for cross-language matrices where exact Python schemas/examples
 * are the behavior under test.
 */
export function servedActionCatalog(): ActionCatalogPayload {
  if (servedActionCatalogCache) return servedActionCatalogCache;

  const python = servedActionCatalogPython();
  if (!existsSync(python)) {
    throw new Error(
      `Backend-served action catalog requires ${python}; `
      + 'set FRISKET_TEST_PYTHON to an exact-lock environment interpreter',
    );
  }

  const sourcePath = resolve(repositoryRoot, 'src');
  const pythonPath = process.env.PYTHONPATH
    ? `${sourcePath}${delimiter}${process.env.PYTHONPATH}`
    : sourcePath;
  const result = spawnSync(
    python,
    ['scripts/ci/gen_action_presentation_catalog_fixture.py'],
    {
      cwd: repositoryRoot,
      encoding: 'utf8',
      env: {
        ...process.env,
        FRISKET_CHECKLOG_DISABLE: '1',
        PYTHONPATH: pythonPath,
      },
      maxBuffer: 4 * 1024 * 1024,
    },
  );
  if (result.status !== 0 || !result.stdout) {
    throw new Error(
      `Backend-served action catalog failed: ${result.error?.message ?? result.stderr}`,
    );
  }

  servedActionCatalogCache = JSON.parse(result.stdout) as ActionCatalogPayload;
  return servedActionCatalogCache;
}
