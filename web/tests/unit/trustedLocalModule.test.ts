import { expect, it } from 'vitest';
import { loadTrustedLocalPluginExport } from '../../src/workbench/trustedLocalModule';

const url = new URL('../support/trustedLocalExports.ts', import.meta.url).href;
it('requires the exact declared action editor export', async () => {
  expect(await loadTrustedLocalPluginExport(url, 'ExactEditor', { exact: true })).toBeTypeOf('function');
  await expect(loadTrustedLocalPluginExport(url, 'Missing', { exact: true })).rejects.toThrow('Missing trusted-local export');
  await expect(loadTrustedLocalPluginExport(url, 'owner.ExactEditor', { exact: true })).rejects.toThrow('Missing trusted-local export');
});
it('preserves the existing legacy workbench export resolution', async () => {
  expect(await loadTrustedLocalPluginExport(url, 'owner.ExactEditor')).toBeTypeOf('function');
  expect(await loadTrustedLocalPluginExport(url, 'Missing')).toBeTypeOf('function');
});
