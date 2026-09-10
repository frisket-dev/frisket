import { execFileSync } from 'node:child_process';
import {
  cpSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const repoRoot = resolve(webRoot, '..');
const sdkRoot = resolve(repoRoot, 'sdk');
const fixtureRoot = resolve(webRoot, 'tests/fixtures/frontend-host-consumer');
const scratch = mkdtempSync(join(tmpdir(), 'frisket-frontend-host-'));

function npm(args, cwd) {
  return execFileSync(process.platform === 'win32' ? 'npm.cmd' : 'npm', args, {
    cwd,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'inherit'],
  });
}

function pack(cwd) {
  const output = npm(['pack', '--json', '--pack-destination', scratch], cwd);
  const result = JSON.parse(output);
  if (!Array.isArray(result) || result.length !== 1 || !result[0].filename) {
    throw new Error(`Unexpected npm pack result from ${cwd}`);
  }
  return resolve(scratch, result[0].filename);
}

try {
  const hostPackage = JSON.parse(
    readFileSync(resolve(webRoot, 'package.json'), 'utf8'),
  );
  const peers = hostPackage.peerDependencies ?? {};
  if (!peers.vite) {
    throw new Error('Frontend host must declare its Vite source-package peer');
  }
  const fixtureSource = readFileSync(resolve(fixtureRoot, 'src/main.tsx'), 'utf8');
  if (/from\s+['"]\.{1,2}\//.test(fixtureSource)) {
    throw new Error('External consumer fixture must import only package exports');
  }

  npm(['run', 'build'], sdkRoot);
  const sdkTarball = pack(sdkRoot);
  const hostTarball = pack(webRoot);

  const consumerRoot = resolve(scratch, 'consumer');
  cpSync(fixtureRoot, consumerRoot, { recursive: true });
  writeFileSync(resolve(consumerRoot, 'package.json'), `${JSON.stringify({
    name: 'frisket-frontend-host-external-consumer',
    private: true,
    type: 'module',
    scripts: { build: 'tsc --noEmit && vite build' },
    dependencies: {
      '@frisket/frontend-host': `file:${hostTarball}`,
      '@frisket/plugin-sdk': `file:${sdkTarball}`,
      react: peers.react,
      'react-dom': peers['react-dom'],
    },
    devDependencies: {
      '@types/react': '^18.3.31',
      '@types/react-dom': '^18.3.7',
      typescript: '~6.0.2',
    },
  }, null, 2)}\n`);
  npm(['install', '--ignore-scripts', '--no-audit', '--no-fund'], consumerRoot);
  npm(['ls', 'vite', '--omit=dev'], consumerRoot);
  npm(['run', 'build'], consumerRoot);
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
