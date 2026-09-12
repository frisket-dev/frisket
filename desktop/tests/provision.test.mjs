import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { constants as fsConstants, promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { prepareRuntime } from '../src/provision.mjs';

async function fixture({ corruptRequirements = false, delay = false } = {}) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'frisket-provision-'));
  const resources = path.join(root, 'resources');
  const data = path.join(root, 'data');
  const bin = path.join(resources, 'bin');
  const runtime = path.join(resources, 'python', 'frisket', 'runtime');
  await fs.mkdir(bin, { recursive: true });
  await fs.mkdir(runtime, { recursive: true });
  const requirements = 'demo==1.0 --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n';
  await fs.writeFile(path.join(resources, 'requirements.txt'), corruptRequirements ? `${requirements}changed` : requirements);
  const digest = createHash('sha256').update(requirements).digest('hex');
  await fs.writeFile(path.join(resources, 'runtime.json'), JSON.stringify({
    schema: 1,
    pythonVersion: '3.12.13',
    uvVersion: '0.11.29',
    requirementsSha256: digest,
    playwrightVersion: '1.62.0',
  }));
  await fs.writeFile(path.join(runtime, '_bootstrap.py'), '# fake bootstrap\n');
  await fs.writeFile(path.join(runtime, '_guard.py'), '# fake guard\n');
  const trace = path.join(root, 'trace.jsonl');
  const fake = `#!${process.execPath}
const fs = require('node:fs');
const path = require('node:path');
const trace = ${JSON.stringify(trace)};
const args = process.argv.slice(2);
fs.appendFileSync(trace, JSON.stringify({ args, pythonpath: process.env.PYTHONPATH, nodeOptions: process.env.NODE_OPTIONS, cache: process.env.UV_CACHE_DIR }) + '\\n');
if (fs.existsSync(${JSON.stringify(path.join(root, 'delay'))})) setTimeout(() => process.exit(0), 5000);
else if (args.includes('sync') && fs.existsSync(${JSON.stringify(path.join(root, 'fail-sync-once'))})) { fs.unlinkSync(${JSON.stringify(path.join(root, 'fail-sync-once'))}); process.stderr.write('token=should-not-escape\\n'); process.exit(17); }
else {
  if (args[0] === 'venv') {
    const python = path.join(args[1], 'bin', 'python');
    fs.mkdirSync(path.dirname(python), { recursive: true });
    fs.copyFileSync(process.argv[1], python);
    fs.chmodSync(python, 0o755);
  }
  process.exit(0);
}`;
  for (const name of ['uv', 'ffmpeg', 'ffprobe', 'deno']) {
    const file = path.join(bin, name);
    await fs.writeFile(file, name === 'uv' ? fake : '#!/bin/sh\nexit 0\n', { mode: 0o755 });
  }
  return {
    root, resources, data, trace,
    cleanup: () => fs.rm(root, { recursive: true, force: true }),
  };
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
    assert.deepEqual(all[0].args.slice(0, 5), ['python', 'install', '3.12.13', '--managed-python', '--no-config']);
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

test('cancels and waits for an owned provisioning process', async () => {
  const subject = await fixture({ delay: true });
  const controller = new AbortController();
  try {
    const pending = prepareRuntime({ resourcesPath: subject.resources, dataPath: subject.data, signal: controller.signal });
    setTimeout(() => controller.abort(), 50);
    await assert.rejects(pending, (error) => error.name === 'AbortError');
  } finally {
    await subject.cleanup();
  }
});
