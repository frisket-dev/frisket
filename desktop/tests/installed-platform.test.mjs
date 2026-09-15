import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import path from 'node:path';
import test from 'node:test';
import { alivePids, runningExecutable } from '../e2e/installed-platform.mjs';

function within(promise, timeoutMs, message) {
  let timeout;
  return Promise.race([
    promise,
    new Promise((_, reject) => { timeout = setTimeout(() => reject(new Error(message)), timeoutMs); }),
  ]).finally(() => clearTimeout(timeout));
}

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
    '$child = [System.Diagnostics.Process]::Start("cmd.exe", "/d /c exit 0"); $child.WaitForExit(); [Console]::Out.WriteLine($child.Id); [Console]::Out.Flush(); Start-Sleep -Seconds 30',
  ], { stdio: ['ignore', 'pipe', 'pipe'] });
  let stderr = '';
  controller.stderr.on('data', (chunk) => { stderr += chunk; });
  try {
    const [chunk] = await within(once(controller.stdout, 'data'), 10_000, 'Windows process-handle controller did not report its exited child.');
    const exitedPid = Number(String(chunk).trim());
    assert.ok(Number.isInteger(exitedPid) && exitedPid > 0, stderr);
    assert.ok(Number.isInteger(controller.pid) && controller.pid > 0);
    assert.deepEqual(await alivePids([controller.pid]), [controller.pid]);
    assert.deepEqual(await alivePids([exitedPid]), []);
  } finally {
    if (controller.exitCode === null) {
      const closed = once(controller, 'close');
      controller.kill();
      await closed;
    }
  }
});
