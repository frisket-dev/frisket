import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { updatePreferences } from '../src/update-preferences.mjs';

test('a skipped version and automatic-check choice survive a new preferences instance', (t) => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'frisket-update-preferences-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const filename = path.join(directory, 'update-preferences.json');
  const first = updatePreferences(filename);
  assert.equal(first.getSkippedVersion(), undefined);
  assert.equal(first.getAutomaticChecks(), undefined);
  first.skipVersion('0.1.1-alpha.83');
  first.setAutomaticChecks(false);
  const restarted = updatePreferences(filename);
  assert.equal(restarted.getSkippedVersion(), '0.1.1-alpha.83');
  assert.equal(restarted.getAutomaticChecks(), false);
  restarted.skipVersion('0.1.1-alpha.84');
  restarted.setAutomaticChecks(true);
  const final = updatePreferences(filename);
  assert.equal(final.getSkippedVersion(), '0.1.1-alpha.84');
  assert.equal(final.getAutomaticChecks(), true);
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
  assert.equal(updatePreferences(filename).getAutomaticChecks(), undefined);
});

test('saving the automatic-check choice retains a skipped version from an earlier preference file', (t) => {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'frisket-update-preferences-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const filename = path.join(directory, 'update-preferences.json');
  writeFileSync(filename, JSON.stringify({ skippedVersion: '0.1.1-alpha.83' }));
  const preferences = updatePreferences(filename);
  preferences.setAutomaticChecks(false);
  const restarted = updatePreferences(filename);
  assert.equal(restarted.getSkippedVersion(), '0.1.1-alpha.83');
  assert.equal(restarted.getAutomaticChecks(), false);
});
