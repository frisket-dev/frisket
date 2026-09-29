import assert from 'node:assert/strict';
import { execFile, spawn } from 'node:child_process';
import { once } from 'node:events';
import path from 'node:path';
import test from 'node:test';
import { promisify } from 'node:util';
import { aliveProcesses, descendants, runningExecutable, selectDescendants } from '../e2e/installed-platform.mjs';

function within(promise, timeoutMs, message) {
  let timeout;
  return Promise.race([
    promise,
    new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error(message)), timeoutMs); }),
  ]).finally(() => clearTimeout(timeout));
}

test('process discovery excludes stale Windows parent IDs and their unrelated subtree', () => {
  const rows = [
    { pid: 30, parent: 20, started: '2026-09-29T12:00:02Z' },
    { pid: 10, parent: 1, started: '2026-09-29T12:00:00Z' },
    { pid: 20, parent: 10, started: '2026-09-29T12:00:00Z' },
    // PID 10 used to belong to the parent of this still-running process.
    { pid: 40, parent: 10, started: '2026-09-29T11:00:00Z' },
    { pid: 50, parent: 40, started: '2026-09-29T12:00:03Z' },
    // Check stale parentage at intermediate nodes, not only the root.
    { pid: 60, parent: 20, started: '2026-09-29T11:30:00Z' },
  ];
  assert.deepEqual(selectDescendants(rows, 10).map(({ pid }) => pid), [10, 20, 30]);
  assert.throws(() => selectDescendants(rows, 99), /disappeared/);
});

test('Windows liveness checks the process instance, not just the PID', { skip: process.platform !== 'win32' }, async () => {
  const current = (await descendants(process.pid)).find(({ pid }) => pid === process.pid);
  assert.ok(current.started);
  assert.deepEqual(await aliveProcesses([current]), [current]);
  assert.deepEqual(await aliveProcesses([{ ...current, started: '2000-01-01T00:00:00.0000000Z' }]), []);
});

test('Windows installer completion observes the exact running executable', { skip: process.platform !== 'win32' }, async () => {
  assert.ok((await runningExecutable(process.execPath)).includes(process.pid));
  assert.deepEqual(await runningExecutable(path.join(path.dirname(process.execPath), 'not-an-installed-program.exe')), []);
});

test('Windows liveness distinguishes a running process from an exited process with a retained handle', { skip: process.platform !== 'win32' }, async () => {
  // The controller keeps its System.Diagnostics.Process handle after the short
  // child exits. Windows retains the exited process's administrative record,
  // which must not count as a live application process.
  const controller = spawn('powershell.exe', [
    '-NoLogo', '-NoProfile', '-NonInteractive', '-Command',
    '$child = [System.Diagnostics.Process]::Start("powershell.exe", "-NoProfile -NonInteractive -Command Start-Sleep -Seconds 30"); [Console]::Out.WriteLine($child.Id); [Console]::Out.Flush(); [Console]::ReadLine() | Out-Null; $child.Kill(); $child.WaitForExit(); [Console]::Out.WriteLine("exited"); [Console]::Out.Flush(); Start-Sleep -Seconds 30',
  ], { stdio: ['pipe', 'pipe', 'pipe'] });
  let stderr = '';
  controller.stderr.on('data', (chunk) => { stderr += chunk; });
  try {
    const [chunk] = await within(once(controller.stdout, 'data'), 10_000, 'Windows process-handle controller did not report its exited child.');
    const exitedPid = Number(String(chunk).trim());
    assert.ok(Number.isInteger(exitedPid) && exitedPid > 0, stderr);
    assert.ok(Number.isInteger(controller.pid) && controller.pid > 0);
    const owned = await descendants(controller.pid);
    const childRecord = owned.find(({ pid }) => pid === exitedPid);
    const controllerRecord = owned.find(({ pid }) => pid === controller.pid);
    assert.ok(childRecord, stderr);
    assert.deepEqual(await aliveProcesses([childRecord]), [childRecord]);
    const exited = once(controller.stdout, 'data');
    controller.stdin.end('\n');
    const [acknowledgement] = await within(exited, 10_000, 'Controller did not acknowledge child exit.');
    assert.equal(String(acknowledgement).trim(), 'exited');
    assert.deepEqual(await aliveProcesses([controllerRecord]), [controllerRecord]);
    assert.deepEqual(await aliveProcesses([childRecord]), []);
  } finally {
    if (controller.exitCode === null) {
      const closed = once(controller, 'close');
      // Also clean the child if an assertion failed before the exit handshake.
      await promisify(execFile)('taskkill.exe', ['/PID', String(controller.pid), '/T', '/F']);
      await closed;
    }
  }
});
