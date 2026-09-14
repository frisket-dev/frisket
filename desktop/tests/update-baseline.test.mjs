import assert from 'node:assert/strict';
import { copyFile, mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

test('baseline packaging reuses resources and restores release metadata after failure', async (t) => {
  const desktop = await mkdtemp(path.join(os.tmpdir(), 'frisket-baseline-'));
  t.after(() => rm(desktop, { recursive: true, force: true }));
  for (const directory of ['scripts', 'resources', 'node_modules/electron-builder/out/cli']) {
    await mkdir(path.join(desktop, directory), { recursive: true });
  }
  await copyFile(new URL('../scripts/package-update-baseline.mjs', import.meta.url), path.join(desktop, 'scripts/package-update-baseline.mjs'));
  const original = '{"version":"0.1.1-alpha.78","pythonPackageVersion":"0.1.1a78","revision":"release-sha"}\n';
  await writeFile(path.join(desktop, 'resources/build.json'), original);
  await writeFile(path.join(desktop, 'node_modules/electron-builder/package.json'), '{"name":"electron-builder"}');
  await writeFile(path.join(desktop, 'node_modules/electron-builder/out/cli/cli.js'), `
    const fs = require('node:fs');
    fs.writeFileSync('observed.json', JSON.stringify({
      build: JSON.parse(fs.readFileSync('resources/build.json')),
      args: process.argv.slice(2),
    }));
    process.exit(19);
  `);
  const result = spawnSync(process.execPath, [path.join(desktop, 'scripts/package-update-baseline.mjs'), 'windows'], { encoding: 'utf8' });
  assert.notEqual(result.status, 0);
  assert.equal(await readFile(path.join(desktop, 'resources/build.json'), 'utf8'), original);
  const observed = JSON.parse(await readFile(path.join(desktop, 'observed.json'), 'utf8'));
  assert.equal(observed.build.version, '0.0.0');
  assert.equal(observed.build.pythonPackageVersion, '0.1.1a78');
  assert.ok(observed.args.includes('--config.directories.output=update-baseline'));
  assert.ok(observed.args.includes('--config.extraMetadata.version=0.0.0'));
});
