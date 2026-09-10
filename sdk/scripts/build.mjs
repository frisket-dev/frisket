import { build } from 'esbuild';
import { execFileSync } from 'node:child_process';
import { copyFileSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const sdkRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const repoRoot = resolve(sdkRoot, '..');

execFileSync(process.execPath, [resolve(sdkRoot, 'scripts/gen-contracts.mjs')], {
  stdio: 'inherit',
});

await build({
  entryPoints: [resolve(sdkRoot, 'src/index.ts')],
  outfile: resolve(sdkRoot, 'dist/index.mjs'),
  bundle: true,
  format: 'esm',
  platform: 'neutral',
  target: 'es2022',
});

execFileSync(
  resolve(sdkRoot, 'node_modules/.bin/tsc'),
  ['--project', resolve(sdkRoot, 'tsconfig.json')],
  { stdio: 'inherit' },
);

mkdirSync(resolve(repoRoot, 'src/frisket/data'), { recursive: true });
copyFileSync(
  resolve(sdkRoot, 'dist/index.mjs'),
  resolve(repoRoot, 'src/frisket/data/plugin_sdk.mjs'),
);
console.log('sdk build complete: contracts.gen.ts, dist/index.mjs, dist/index.d.ts, src/frisket/data/plugin_sdk.mjs');
