import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { constants as fsConstants, promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { prepareRuntime } from '../src/provision.mjs';

async function fixture({ corruptRequirements = false, pythonVersion = '3.12.13', realGuard = false, ignoreTermGrandchild = false, largeRapidocr = false } = {}) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'frisket-provision-'));
  const resources = path.join(root, 'resources');
  const data = path.join(root, 'data');
  const bin = path.join(resources, 'bin');
  const runtime = path.join(resources, 'python', 'frisket', 'runtime');
  await fs.mkdir(bin, { recursive: true });
  await fs.mkdir(runtime, { recursive: true });
  await fs.mkdir(path.join(resources, 'model-cache', 'huggingface'), { recursive: true });
  await fs.mkdir(path.join(resources, 'model-cache', 'models', 'rapidocr'), { recursive: true });
  await fs.writeFile(path.join(resources, 'model-cache', 'huggingface', 'whisper-base'), 'bundled-whisper');
  await fs.symlink('whisper-base', path.join(resources, 'model-cache', 'huggingface', 'whisper-link'));
  const rapidocrModel = path.join(resources, 'model-cache', 'models', 'rapidocr', 'default-v5.onnx');
  if (largeRapidocr) {
    const bytes = Buffer.alloc(64 * 1024 * 1024, 0x61);
    bytes[bytes.length - 1] = 0x7a;
    await fs.writeFile(rapidocrModel, bytes);
  } else {
    await fs.writeFile(rapidocrModel, 'bundled-rapidocr');
  }
  const requirements = 'demo==1.0 --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n';
  await fs.writeFile(path.join(resources, 'requirements.txt'), corruptRequirements ? `${requirements}changed` : requirements);
  const digest = createHash('sha256').update(requirements).digest('hex');
  await fs.writeFile(path.join(resources, 'runtime.json'), JSON.stringify({
    schema: 1,
    pythonVersion,
    uvVersion: '0.11.29',
    requirementsSha256: digest,
    playwrightVersion: '1.62.0',
  }));
  await fs.writeFile(path.join(runtime, '_bootstrap.py'), '# fake bootstrap\n');
  const guard = path.join(runtime, '_guard.py');
  if (realGuard) await fs.copyFile(new URL('../../src/frisket/runtime/_guard.py', import.meta.url), guard);
  else await fs.writeFile(guard, '# fake guard\n');
  const trace = path.join(root, 'trace.jsonl');
  const grandchildPid = path.join(root, 'term-ignoring-grandchild.pid');
  const grandchildReady = path.join(root, 'term-ignoring-grandchild.ready');
  const guardPython = realGuard ? execFileSync('python3', ['-c', 'import sys; print(sys.executable)'], { encoding: 'utf8' }).trim() : '';
  const fake = `#!${process.execPath}
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const trace = ${JSON.stringify(trace)};
const args = process.argv.slice(2);
fs.appendFileSync(trace, JSON.stringify({ args, pythonpath: process.env.PYTHONPATH, nodeOptions: process.env.NODE_OPTIONS, cache: process.env.UV_CACHE_DIR }) + '\\n');
if (args.includes('sync') && ${JSON.stringify(ignoreTermGrandchild)}) {
  const descendant = spawn(process.execPath, ['-e', ${JSON.stringify(`const fs = require('node:fs'); process.on('SIGTERM', () => {}); fs.writeFileSync(${JSON.stringify(grandchildReady)}, 'ready'); setInterval(() => {}, 1000)`)}], { stdio: 'ignore' });
  fs.writeFileSync(${JSON.stringify(grandchildPid)}, String(descendant.pid));
  setInterval(() => {}, 1000);
} else if (args.includes('sync') && fs.existsSync(${JSON.stringify(path.join(root, 'fail-sync-once'))})) { fs.unlinkSync(${JSON.stringify(path.join(root, 'fail-sync-once'))}); process.stderr.write('token=should-not-escape\\n'); process.exit(17); }
else {
  if (args[0] === 'venv') {
    const python = path.join(args[1], 'bin', 'python');
    fs.mkdirSync(path.dirname(python), { recursive: true });
    if (${JSON.stringify(realGuard)}) fs.writeFileSync(python, '#!/bin/sh\\nexec ' + ${JSON.stringify(JSON.stringify(guardPython))} + ' "$@"\\n');
    else fs.copyFileSync(process.argv[1], python);
    fs.chmodSync(python, 0o755);
  }
  process.exit(0);
}`;
  for (const name of ['uv', 'ffmpeg', 'ffprobe', 'deno']) {
    const file = path.join(bin, name);
    await fs.writeFile(file, name === 'uv' ? fake : '#!/bin/sh\nexit 0\n', { mode: 0o755 });
  }
  return {
    root, resources, data, trace, grandchildPid, grandchildReady,
    cleanup: () => fs.rm(root, { recursive: true, force: true }),
  };
}

async function waitForFile(filename) {
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    try {
      return await fs.readFile(filename, 'utf8');
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error(`timed out waiting for ${filename}`);
}

async function waitForModelCacheTemporary(directory) {
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    try {
      const names = await fs.readdir(directory);
      if (names.some((name) => name.includes('.frisket-model-cache-') && name.endsWith('.tmp'))) return;
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
  throw new Error('timed out waiting for model cache publication');
}

async function traceLines(filename) {
  try {
    return (await fs.readFile(filename, 'utf8')).trim().split('\n').filter(Boolean).map(JSON.parse);
  } catch (error) {
    if (error.code === 'ENOENT') return [];
    throw error;
  }
}

test('refuses requirements whose digest differs from the manifest', async () => {
  const subject = await fixture({ corruptRequirements: true });
  try {
    await assert.rejects(
      prepareRuntime({ resourcesPath: subject.resources, dataPath: subject.data }),
      /integrity verification/,
    );
    assert.deepEqual(await traceLines(subject.trace), []);
  } finally {
    await subject.cleanup();
  }
});

test('refuses malformed runtime versions before starting a process', async () => {
  const subject = await fixture({ pythonVersion: '3.12.13;unexpected' });
  try {
    await assert.rejects(
      prepareRuntime({ resourcesPath: subject.resources, dataPath: subject.data }),
      /unsupported format/,
    );
    assert.deepEqual(await traceLines(subject.trace), []);
  } finally {
    await subject.cleanup();
  }
});

test('repairs an incomplete environment and records completion only after every command', async () => {
  const subject = await fixture();
  const oldPythonPath = process.env.PYTHONPATH;
  const oldNodeOptions = process.env.NODE_OPTIONS;
  process.env.PYTHONPATH = '/unsafe/python';
  process.env.NODE_OPTIONS = '--require=/unsafe/node';
  try {
    await fs.writeFile(path.join(subject.root, 'fail-sync-once'), 'yes');
    await assert.rejects(
      prepareRuntime({ resourcesPath: subject.resources, dataPath: subject.data }),
      /installing private Python dependencies.*token=\[redacted\]/,
    );
    const first = await traceLines(subject.trace);
    assert.equal(first.some(({ args }) => args.includes('sync')), true);
    assert.equal(first.some(({ args }) => args.includes('playwright')), false);

    const ready = await prepareRuntime({ resourcesPath: subject.resources, dataPath: subject.data });
    await fs.access(ready.python, fsConstants.X_OK);
    const all = await traceLines(subject.trace);
    const venvCalls = all.filter(({ args }) => args[0] === 'venv');
    assert.deepEqual(all[0].args.slice(0, 6), ['python', 'install', '3.12.13', '--managed-python', '--no-bin', '--no-config']);
    assert.equal(venvCalls.length, 2);
    assert.equal(venvCalls[1].args.includes('--clear'), true);
    const sync = all.find(({ args }) => args.includes('sync'));
    assert.deepEqual(sync.args.slice(0, 6), ['-I', path.join(subject.resources, 'python', 'frisket', 'runtime', '_guard.py'), String(process.pid), '2', path.join(subject.resources, 'bin', 'uv'), 'pip']);
    assert.equal(sync.args.includes('--require-hashes'), true);
    assert.equal(all.some(({ args }) => args.includes('playwright') && args.includes('chromium')), true);
    assert.equal(all.some(({ args }) => args.includes('runtime-info')), true);
    assert.equal(all.every(({ pythonpath, nodeOptions }) => pythonpath === undefined && nodeOptions === undefined), true);
    assert.equal(ready.env.FRISKET_FFPROBE_PATH, path.join(subject.resources, 'bin', 'ffprobe'));
    assert.equal(ready.env.PLAYWRIGHT_BROWSERS_PATH.endsWith(path.join('cache', 'playwright', '1.62.0')), true);

    const beforeReuse = all.length;
    await prepareRuntime({ resourcesPath: subject.resources, dataPath: subject.data });
    assert.equal((await traceLines(subject.trace)).length, beforeReuse, 'a completed environment is reused without reinstalling application code');
  } finally {
    if (oldPythonPath === undefined) delete process.env.PYTHONPATH;
    else process.env.PYTHONPATH = oldPythonPath;
    if (oldNodeOptions === undefined) delete process.env.NODE_OPTIONS;
    else process.env.NODE_OPTIONS = oldNodeOptions;
    await subject.cleanup();
  }
});

test('seeds bundled local model caches without replacing an existing user cache file', async () => {
  const subject = await fixture();
  try {
    await prepareRuntime({ resourcesPath: subject.resources, dataPath: subject.data });
    const whisper = path.join(subject.data, 'cache', 'huggingface', 'whisper-base');
    const whisperLink = path.join(subject.data, 'cache', 'huggingface', 'whisper-link');
    const rapidocr = path.join(subject.data, 'cache', 'models', 'rapidocr', 'default-v5.onnx');
    assert.equal(await fs.readFile(whisper, 'utf8'), 'bundled-whisper');
    assert.equal(await fs.readlink(whisperLink), 'whisper-base');
    assert.equal(await fs.readFile(rapidocr, 'utf8'), 'bundled-rapidocr');

    await fs.writeFile(rapidocr, 'user-cache');
    await prepareRuntime({ resourcesPath: subject.resources, dataPath: subject.data });
    assert.equal(await fs.readFile(rapidocr, 'utf8'), 'user-cache');
  } finally {
    await subject.cleanup();
  }
});

test('cancelling a bundled model copy leaves no partial cache file and retries cleanly', async () => {
  const subject = await fixture({ largeRapidocr: true });
  const controller = new AbortController();
  const rapidocrDirectory = path.join(subject.data, 'cache', 'models', 'rapidocr');
  const rapidocr = path.join(rapidocrDirectory, 'default-v5.onnx');
  try {
    const pending = prepareRuntime({
      resourcesPath: subject.resources,
      dataPath: subject.data,
      signal: controller.signal,
    });
    pending.catch(() => {});
    await waitForModelCacheTemporary(rapidocrDirectory);
    controller.abort();
    await assert.rejects(pending, (error) => error.name === 'AbortError');
    await assert.rejects(fs.access(rapidocr), { code: 'ENOENT' });
    assert.equal((await fs.readdir(rapidocrDirectory)).some((name) => name.includes('.frisket-model-cache-')), false);

    await prepareRuntime({ resourcesPath: subject.resources, dataPath: subject.data });
    const [source, seeded] = await Promise.all([
      fs.readFile(path.join(subject.resources, 'model-cache', 'models', 'rapidocr', 'default-v5.onnx')),
      fs.readFile(rapidocr),
    ]);
    assert.deepEqual(seeded, source);
  } finally {
    controller.abort();
    await subject.cleanup();
  }
});

test('the bundled guardian reaps a TERM-ignoring descendant before cancellation resolves', async () => {
  const subject = await fixture({ realGuard: true, ignoreTermGrandchild: true });
  const controller = new AbortController();
  let pending;
  try {
    pending = prepareRuntime({ resourcesPath: subject.resources, dataPath: subject.data, signal: controller.signal });
    pending.catch(() => {});
    const grandchild = Number(await waitForFile(subject.grandchildPid));
    await waitForFile(subject.grandchildReady);
    const started = Date.now();
    controller.abort();
    await assert.rejects(pending, (error) => error.name === 'AbortError');
    assert.ok(Date.now() - started >= 1_800, 'the guardian received its full two-second cleanup grace');
    assert.throws(() => process.kill(grandchild, 0), (error) => error.code === 'ESRCH');
  } finally {
    controller.abort();
    await pending?.catch(() => {});
    await subject.cleanup();
  }
});
