import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { spawnWindowsOwned, requestWindowsStop } from '../src/windows-owned.mjs';

// A native Job contract: POSIX tests cannot establish Windows kernel ownership.
test('Windows guardian preserves stdin/argv and acknowledges descendant teardown', { skip: process.platform !== 'win32' }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'frisket job '));
  const runtime = path.join(root, 'python', 'frisket', 'runtime');
  await fs.mkdir(runtime, { recursive: true });
  await fs.copyFile(new URL('../../src/frisket/runtime/_guard_windows.ps1', import.meta.url), path.join(runtime, '_guard_windows.ps1'));
  const marker = path.join(root, 'late-marker');
  const leaf = path.join(root, 'leaf.cjs');
  const target = path.join(root, 'target with spaces.cjs');
  await fs.writeFile(leaf, `setTimeout(() => require('node:fs').writeFileSync(process.argv[2], 'leaked'), 2500); console.log('leaf-ready');`);
  await fs.writeFile(target, `
const child = require('node:child_process').spawn(process.execPath, [${JSON.stringify(leaf)}, ${JSON.stringify(marker)}], { stdio: ['ignore', 'inherit', 'inherit'] });
process.stdin.once('data', data => console.log(JSON.stringify([data.toString(), ...process.argv.slice(2)])));
setInterval(() => {}, 1000);
`);
  let child;
  try {
    child = await spawnWindowsOwned(process.execPath, [target, 'spaces and "quotes"', 'trailing\\', '', '日本語'], {
      resourcesPath: root, env: process.env, stdio: ['pipe', 'pipe', 'pipe'],
    });
    let output = '';
    let stderr = '';
    child.stdout.on('data', chunk => { output += chunk; });
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.stdin.end('private-startup-record');
    const deadline = Date.now() + 30000;
    while (!output.includes('leaf-ready') || !output.includes('private-startup-record')) {
      if (child.exitCode !== null || Date.now() > deadline) throw new Error(`guardian failed: ${stderr}`);
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    assert.ok(output.includes(JSON.stringify(['private-startup-record', 'spaces and "quotes"', 'trailing\\', '', '日本語'])));
    const closed = new Promise(resolve => child.once('close', resolve));
    await requestWindowsStop(child);
    await closed;
    assert.equal(await child.windowsCleanupProof, true);
    await new Promise(resolve => setTimeout(resolve, 2800));
    await assert.rejects(fs.access(marker), { code: 'ENOENT' });
  } finally {
    child?.kill();
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('malformed Windows launch requests are refused before any process starts', async () => {
  await assert.rejects(spawnWindowsOwned('relative', [], { resourcesPath: '/resources', env: {}, stdio: 'ignore' }), /invalid Windows runtime/);
  await assert.rejects(spawnWindowsOwned(process.execPath, ['nul\0argument'], { resourcesPath: process.cwd(), env: {}, stdio: 'ignore' }), /invalid Windows runtime/);
});

test('abrupt application-owner death tears down Windows Job descendants', { skip: process.platform !== 'win32' }, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'frisket-owner-'));
  const runtime = path.join(root, 'python', 'frisket', 'runtime');
  await fs.mkdir(runtime, { recursive: true });
  await fs.copyFile(new URL('../../src/frisket/runtime/_guard_windows.ps1', import.meta.url), path.join(runtime, '_guard_windows.ps1'));
  const marker = path.join(root, 'late-marker');
  const target = path.join(root, 'target.cjs');
  const leaf = path.join(root, 'leaf.cjs');
  await fs.writeFile(leaf, `console.log('leaf-ready'); setTimeout(() => require('node:fs').writeFileSync(process.argv[2], 'leaked'), 2500);`);
  await fs.writeFile(target, `require('node:child_process').spawn(process.execPath, [${JSON.stringify(leaf)}, ${JSON.stringify(marker)}], { stdio: ['ignore', 'inherit', 'inherit'] }); setInterval(() => {}, 1000);`);
  const controller = path.join(root, 'owner.mjs');
  const helper = new URL('../src/windows-owned.mjs', import.meta.url).href;
  await fs.writeFile(controller, `
import { spawnWindowsOwned } from ${JSON.stringify(helper)};
const child = await spawnWindowsOwned(process.execPath, [${JSON.stringify(target)}], { resourcesPath: ${JSON.stringify(root)}, env: process.env, stdio: ['ignore', 'pipe', 'inherit'] });
child.stdout.once('data', () => { console.log('owner-ready'); process.exit(0); });
`);
  const owner = spawn(process.execPath, [controller], { stdio: ['ignore', 'pipe', 'pipe'] });
  let output = '';
  let stderr = '';
  owner.stdout.on('data', chunk => { output += chunk; });
  owner.stderr.on('data', chunk => { stderr += chunk; });
  try {
    const deadline = Date.now() + 30000;
    while (owner.exitCode === null && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 20));
    assert.equal(owner.exitCode, 0, stderr);
    assert.ok(output.includes('owner-ready'), stderr);
    await new Promise(resolve => setTimeout(resolve, 2800));
    await assert.rejects(fs.access(marker), { code: 'ENOENT' });
  } finally {
    owner.kill();
    await fs.rm(root, { recursive: true, force: true });
  }
});
