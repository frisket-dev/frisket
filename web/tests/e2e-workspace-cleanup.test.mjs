import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  activeMarkerPath,
  cleanupCurrentE2EWorkspace,
  prepareE2EWorkspace,
  pruneStaleE2EWorkspaces,
} from '../e2e-workspace-cleanup.mjs';

function tempRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'frisket-cleanup-test-'));
}

test('passed isolated e2e runs remove their temp workspace', () => {
  const root = tempRoot();
  const workspace = path.join(root, 'frisket-e2e-pass');
  fs.mkdirSync(workspace);
  const result = cleanupCurrentE2EWorkspace({
    status: 'passed',
    tmpDir: root,
    env: { FRISKET_E2E_ISOLATED: '1', FRISKET_E2E_WS: workspace },
  });
  assert.equal(result.removed, true);
  assert.equal(fs.existsSync(workspace), false);
  fs.rmSync(root, { recursive: true, force: true });
});

test('failed e2e runs keep their workspace by default', () => {
  const root = tempRoot();
  const workspace = path.join(root, 'frisket-e2e-fail');
  fs.mkdirSync(workspace);
  const result = cleanupCurrentE2EWorkspace({
    status: 'failed',
    tmpDir: root,
    env: { FRISKET_E2E_ISOLATED: '1', FRISKET_E2E_WS: workspace },
  });
  assert.deepEqual(result, { removed: false, reason: 'kept' });
  assert.equal(fs.existsSync(workspace), true);
  fs.rmSync(root, { recursive: true, force: true });
});

test('cleanup refuses unsafe workspace paths', () => {
  const root = tempRoot();
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'not-frisket-e2e-'));
  const result = cleanupCurrentE2EWorkspace({
    status: 'passed',
    tmpDir: root,
    env: { FRISKET_E2E_ISOLATED: '1', FRISKET_E2E_WS: workspace },
  });
  assert.deepEqual(result, { removed: false, reason: 'unsafe-path' });
  assert.equal(fs.existsSync(workspace), true);
  fs.rmSync(root, { recursive: true, force: true });
  fs.rmSync(workspace, { recursive: true, force: true });
});

test('stale pruning skips active workspaces', () => {
  const root = tempRoot();
  const oldWorkspace = path.join(root, 'frisket-e2e-old');
  const activeWorkspace = path.join(root, 'frisket-e2e-active');
  fs.mkdirSync(oldWorkspace);
  fs.mkdirSync(activeWorkspace);
  fs.writeFileSync(activeMarkerPath(activeWorkspace), JSON.stringify({ pid: process.pid }));
  const oldTime = new Date(Date.now() - 72 * 60 * 60 * 1000);
  fs.utimesSync(oldWorkspace, oldTime, oldTime);
  fs.utimesSync(activeWorkspace, oldTime, oldTime);

  const removed = pruneStaleE2EWorkspaces({
    tmpDir: root,
    now: Date.now(),
    env: { FRISKET_E2E_PRUNE_HOURS: '1' },
  });

  assert.deepEqual(removed, [oldWorkspace]);
  assert.equal(fs.existsSync(oldWorkspace), false);
  assert.equal(fs.existsSync(activeWorkspace), true);
  fs.rmSync(root, { recursive: true, force: true });
});

test('prepare writes an active marker only for isolated safe workspaces', () => {
  const root = tempRoot();
  const workspace = path.join(root, 'frisket-e2e-prepared');
  prepareE2EWorkspace({
    workspace,
    runId: 'run-1',
    tmpDir: root,
    env: { FRISKET_E2E_ISOLATED: '1' },
  });
  assert.equal(JSON.parse(fs.readFileSync(activeMarkerPath(workspace), 'utf8')).run_id, 'run-1');
  fs.rmSync(root, { recursive: true, force: true });
});
