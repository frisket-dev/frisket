import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const DESKTOP = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

async function validateBuilderConfig(config) {
  const require = createRequire(import.meta.url);
  const builderRequire = createRequire(require.resolve('electron-builder/package.json'));
  const { validateConfiguration } = builderRequire('app-builder-lib/out/util/config/config.js');
  const { DebugLogger } = builderRequire('builder-util');
  await validateConfiguration(config, new DebugLogger());
}

async function fixture() {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'frisket-packaging-'));
  await fs.mkdir(path.join(root, 'resources'));
  await fs.copyFile(path.join(DESKTOP, 'electron-builder.cjs'), path.join(root, 'electron-builder.cjs'));
  await fs.writeFile(path.join(root, 'resources', 'build.json'), JSON.stringify({
    version: '1.2.3', revision: '1234567890abcdef',
  }));
  return root;
}

function loadConfig(root, env = {}) {
  return spawnSync(process.execPath, ['-e',
    'process.stdout.write(JSON.stringify(require(process.argv[1])))',
    path.join(root, 'electron-builder.cjs'),
  ], {
    cwd: root,
    encoding: 'utf8',
    env: { PATH: process.env.PATH, ...env },
  });
}

test('Windows packaging is an explicit per-user x64 NSIS installer', async (t) => {
  const root = await fixture();
  t.after(() => fs.rm(root, { recursive: true, force: true }));

  const result = loadConfig(root);
  assert.equal(result.status, 0, result.stderr);
  const config = JSON.parse(result.stdout);
  assert.deepEqual(config.win.target, [{ target: 'nsis', arch: ['x64'] }]);
  assert.equal(config.win.requestedExecutionLevel, 'asInvoker');
  assert.equal(config.win.signExecutable, false);
  assert.deepEqual(config.nsis, { oneClick: true, perMachine: false, runAfterFinish: false });
  assert.equal(config.forceCodeSigning, false);
  assert.equal(config.mac.identity, '-');
  assert.equal('azureSignOptions' in config.win, false);
});

test('Windows signing requires its own Artifact Signing metadata', async (t) => {
  const root = await fixture();
  t.after(() => fs.rm(root, { recursive: true, force: true }));

  const missing = loadConfig(root, { FRISKET_DESKTOP_WINDOWS_SIGNED_BUILD: '1' });
  assert.notEqual(missing.status, 0);
  assert.match(missing.stderr, /Signed Windows packaging requires/);

  const signed = loadConfig(root, {
    FRISKET_DESKTOP_WINDOWS_SIGNED_BUILD: '1',
    FRISKET_WINDOWS_SIGNING_PUBLISHER_NAME: 'Example Publisher',
    FRISKET_WINDOWS_SIGNING_ENDPOINT: 'https://example.invalid/signing',
    FRISKET_WINDOWS_SIGNING_ACCOUNT: 'account',
    FRISKET_WINDOWS_SIGNING_PROFILE: 'profile',
  });
  assert.equal(signed.status, 0, signed.stderr);
  const config = JSON.parse(signed.stdout);
  assert.equal(config.forceCodeSigning, true);
  assert.equal(config.win.signExecutable, true);
  assert.deepEqual(config.win.azureSignOptions, {
    publisherName: 'Example Publisher',
    endpoint: 'https://example.invalid/signing',
    codeSigningAccountName: 'account',
    certificateProfileName: 'profile',
  });
  assert.equal(config.mac.identity, '-');
  await validateBuilderConfig(config);
});

test('macOS signing retains its separate credential gate', async (t) => {
  const root = await fixture();
  t.after(() => fs.rm(root, { recursive: true, force: true }));

  const result = loadConfig(root, { FRISKET_DESKTOP_SIGNED_BUILD: '1' });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Signed macOS packaging requires/);

  const signed = loadConfig(root, {
    FRISKET_DESKTOP_SIGNED_BUILD: '1',
    CSC_LINK: 'fixture.p12',
    APPLE_API_KEY: 'fixture.p8',
    APPLE_API_KEY_ID: 'fixture-key',
    APPLE_API_ISSUER: 'fixture-issuer',
  });
  assert.equal(signed.status, 0, signed.stderr);
  await validateBuilderConfig(JSON.parse(signed.stdout));
});
