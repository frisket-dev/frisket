import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { localMetadata, serveUpdateFeed } from '../e2e/update-feed.mjs';

test('loopback metadata keeps the release version, digests, sizes and original metadata', () => {
  const release = { version: '1.2.3', path: 'https://github.com/releases/download/v1.2.3/app.exe', sha512: 'digest', files: [{ url: 'app.exe', sha512: 'digest', size: 123 }] };
  const local = localMetadata(release, 'http://127.0.0.1:1234');
  assert.deepEqual(local, { ...release, path: 'http://127.0.0.1:1234/app.exe', files: [{ ...release.files[0], url: 'http://127.0.0.1:1234/app.exe' }] });
  assert.equal(release.files[0].url, 'app.exe');
});

test('loopback feed serves target bytes and refuses unlisted files', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'frisket-update-feed-'));
  let feed;
  try {
    await writeFile(path.join(directory, 'app.zip'), 'signed target bytes');
    await writeFile(path.join(directory, 'private.txt'), 'not an artifact');
    await writeFile(path.join(directory, 'latest-mac.yml'), JSON.stringify({ version: '1.2.3', files: [{ url: 'https://github.com/release/app.zip', sha512: 'digest', size: 19 }] }));
    feed = await serveUpdateFeed(directory, 'darwin');
    const metadata = await (await fetch(`${feed.origin}/latest-mac.yml`)).json();
    assert.equal(await (await fetch(metadata.files[0].url)).text(), 'signed target bytes');
    assert.equal((await fetch(`${feed.origin}/private.txt`)).status, 404);
    assert.equal(metadata.files[0].sha512, 'digest');
    assert.ok(feed.requests.some((request) => request.name === 'app.zip'));
  } finally {
    await feed?.close();
    await rm(directory, { recursive: true, force: true });
  }
});
