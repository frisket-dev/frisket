import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { updatePreferences } from '../src/update-preferences.mjs';

test('a skipped version survives a new preferences instance and can be replaced', (t) => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'frisket-update-preferences-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const filename = path.join(directory, 'update-preferences.json');
  const first = updatePreferences(filename);
  assert.equal(first.getSkippedVersion(), undefined);
  first.skipVersion('0.1.1-alpha.83');
  const restarted = updatePreferences(filename);
  assert.equal(restarted.getSkippedVersion(), '0.1.1-alpha.83');
  restarted.skipVersion('0.1.1-alpha.84');
  assert.equal(updatePreferences(filename).getSkippedVersion(), '0.1.1-alpha.84');
});

test('invalid preferences do not prevent update checks', (t) => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'frisket-update-preferences-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const filename = path.join(directory, 'update-preferences.json');
  t.mock.method(console, 'warn', () => {});
  writeFileSync(filename, '{unfinished');
  assert.equal(updatePreferences(filename).getSkippedVersion(), undefined);
  writeFileSync(filename, JSON.stringify({ skippedVersion: 83 }));
  assert.equal(updatePreferences(filename).getSkippedVersion(), undefined);
});
