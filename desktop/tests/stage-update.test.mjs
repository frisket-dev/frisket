import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { prepareMetadata, stageRelease, verifyConfiguration, verifyMetadata } from '../scripts/stage-update.mjs';

const build = { version: '0.1.1-alpha.78', pythonPackageVersion: '0.1.1a78', revision: 'a'.repeat(40) };
const sha512 = (bytes) => createHash('sha512').update(bytes).digest('base64');

test('packaged configuration enforces the public feed and signed Windows publisher', () => {
  const configuration = { provider: 'generic', channel: 'latest', url: 'https://github.com/frisket-dev/frisket/releases/latest/download/', publisherName: ['Frisket publisher'] };
  verifyConfiguration(configuration, 'Frisket publisher');
  assert.throws(() => verifyConfiguration({ ...configuration, publisherName: undefined }, 'Frisket publisher'), /signing publisher/);
  assert.throws(() => verifyConfiguration({ ...configuration, url: 'https://other.example/' }), /public latest feed/);
});

async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'frisket-update-stage-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const directories = { mac: path.join(root, 'mac'), windows: path.join(root, 'windows') };
  const metadata = {};
  for (const [platform, directory] of Object.entries(directories)) {
    await mkdir(directory);
    const name = `Frisket-Desktop-${build.version}-${platform === 'mac' ? 'arm64' : 'x64'}-aaaaaaaa.${platform === 'mac' ? 'zip' : 'exe'}`;
    const bytes = Buffer.from(`signed ${platform} payload`);
    await writeFile(path.join(directory, name), bytes);
    await writeFile(path.join(directory, `${name}.blockmap`), 'blockmap');
    metadata[platform] = { version: build.version, files: [{ url: name, sha512: sha512(bytes), size: bytes.length }], releaseDate: '2026-09-14T00:00:00Z' };
    if (platform === 'mac') {
      const dmg = name.replace(/\.zip$/, '.dmg');
      await writeFile(path.join(directory, dmg), 'stapled dmg');
      metadata.mac.files.push({ url: dmg, sha512: 'obsolete before stapling' });
    }
  }
  return { root, directories, metadata };
}

test('metadata selects signed updater payload and pins URLs to the release tag', async (t) => {
  const { directories, metadata } = await fixture(t);
  const result = await prepareMetadata('mac', directories.mac, build, metadata.mac);
  assert.equal(result.files.length, 1);
  assert.match(result.files[0].url, /\/releases\/download\/desktop-v0\.1\.1a78\/Frisket-Desktop-0\.1\.1-alpha\.78-arm64-aaaaaaaa\.zip$/);
  assert.equal(result.path, result.files[0].url);
  await verifyMetadata('mac', directories.mac, build, result);
});

test('corrupt payloads, mismatched versions and mutable URLs refuse staging', async (t) => {
  const { directories, metadata } = await fixture(t);
  await assert.rejects(prepareMetadata('windows', directories.windows, build, { ...metadata.windows, version: '0.1.0' }), /version differs/);
  const prepared = await prepareMetadata('windows', directories.windows, build, metadata.windows);
  await assert.rejects(verifyMetadata('windows', directories.windows, build, { ...prepared, path: 'https://github.com/frisket-dev/frisket/releases/latest/download/app.exe' }), /immutable/);
  await writeFile(path.join(directories.windows, metadata.windows.files[0].url), 'changed bytes');
  await assert.rejects(prepareMetadata('windows', directories.windows, build, metadata.windows), /hash or size differs/);
});

test('release staging preserves stable installers, versioned updater assets and checksums', async (t) => {
  const { root, directories, metadata } = await fixture(t);
  for (const platform of ['mac', 'windows']) {
    const prepared = await prepareMetadata(platform, directories[platform], build, metadata[platform]);
    await writeFile(path.join(directories[platform], platform === 'mac' ? 'latest-mac.yml' : 'latest.yml'), JSON.stringify(prepared));
  }
  const destination = path.join(root, 'release');
  const names = await stageRelease(directories.mac, directories.windows, destination, build);
  assert.deepEqual((await readdir(destination)).sort(), names.sort());
  assert.equal(await readFile(path.join(destination, 'Frisket-Desktop-arm64.dmg'), 'utf8'), 'stapled dmg');
  assert.equal(await readFile(path.join(destination, 'Frisket-Desktop-x64.exe'), 'utf8'), 'signed windows payload');
  const checksums = await readFile(path.join(destination, 'SHA256SUMS'), 'utf8');
  for (const name of names.filter((name) => name !== 'SHA256SUMS')) {
    const expected = createHash('sha256').update(await readFile(path.join(destination, name))).digest('hex');
    assert.ok(checksums.includes(`${expected}  ${name}\n`));
  }
  await assert.rejects(stageRelease(directories.mac, directories.windows, destination, build), /must be empty/);
});
