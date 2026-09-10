import { spawnSync } from 'node:child_process';

for (const edition of ['local', 'team']) {
  const result = spawnSync(
    process.platform === 'win32' ? 'npx.cmd' : 'npx',
    ['vite', 'build'],
    {
      cwd: new URL('..', import.meta.url),
      env: { ...process.env, FRISKET_EDITION: edition },
      stdio: 'inherit',
    },
  );
  if (result.status !== 0) process.exit(result.status ?? 1);
}
