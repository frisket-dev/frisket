import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const buildFile = path.join(desktop, 'resources/build.json');
const original = readFileSync(buildFile);
const [platform] = process.argv.slice(2);
if (!['mac', 'windows'].includes(platform)) throw new Error('Pass mac or windows');
const require = createRequire(import.meta.url);

// Reuse prepared resources. Only Electron's fixture version changes; the
// bundled Python package remains the actual release package being validated.
try {
  writeFileSync(buildFile, `${JSON.stringify({ ...JSON.parse(original), version: '0.0.0' }, null, 2)}\n`);
  execFileSync(process.execPath, [
    require.resolve('electron-builder/out/cli/cli.js'),
    '--config', 'electron-builder.cjs',
    ...(platform === 'mac' ? ['--mac', 'zip', '--arm64'] : ['--win', 'nsis', '--x64']),
    '--publish', 'never',
    '--config.extraMetadata.version=0.0.0',
    '--config.directories.output=update-baseline',
  ], { cwd: desktop, stdio: 'inherit' });
} finally {
  writeFileSync(buildFile, original);
}
