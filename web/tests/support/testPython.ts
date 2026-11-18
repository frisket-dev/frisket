import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const REPO_PYTHON = path.join(REPO_ROOT, '.venv/bin/python');

/**
 * The Python interpreter every test shells out to: FRISKET_TEST_PYTHON, else
 * the repository's own `.venv`. A worktree has no `.venv` of its own, so it
 * must name the shared interpreter explicitly.
 */
export function testPython(): string {
  const configured = process.env.FRISKET_TEST_PYTHON;
  if (configured) return configured;
  if (existsSync(REPO_PYTHON)) return REPO_PYTHON;
  throw new Error(
    `FRISKET_TEST_PYTHON is unset and ${REPO_PYTHON} does not exist; ` +
      'set FRISKET_TEST_PYTHON to the interpreter of a synced frisket environment',
  );
}

export function hasTestPython(): boolean {
  try {
    return existsSync(testPython());
  } catch {
    return false;
  }
}
