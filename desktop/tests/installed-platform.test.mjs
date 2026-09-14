import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { runningExecutable } from '../e2e/installed-platform.mjs';

test('Windows installer completion observes the exact running executable', { skip: process.platform !== 'win32' }, async () => {
  assert.ok((await runningExecutable(process.execPath)).includes(process.pid));
  assert.deepEqual(await runningExecutable(path.join(path.dirname(process.execPath), 'not-an-installed-program.exe')), []);
});
