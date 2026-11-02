import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { expect, it } from 'vitest';
import { publishCaptureBundle } from '../e2e/docs/capture';

it('preserves a numeric sample revision in the published bundle', async () => {
  const parent = await mkdtemp(path.join(os.tmpdir(), 'docs-bundle-'));
  const output = path.join(parent, 'bundle');
  try {
    await publishCaptureBundle(output, { appRevision: 'abc', sample: { id: 'filings', revision: 1 } }, async (stage) => {
      await writeFile(path.join(stage, 'setup.png'), 'image');
      return [{ id: 'setup', image: 'setup.png', width: 2, height: 2, scale: 2, annotations: [] }];
    });
    const bundle = JSON.parse(await readFile(path.join(output, 'bundle.json'), 'utf8'));
    expect(bundle.sample).toEqual({ id: 'filings', revision: 1 });
  } finally {
    await rm(parent, { recursive: true, force: true });
  }
});
