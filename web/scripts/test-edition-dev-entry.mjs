import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');

function availablePort() {
  return new Promise((resolvePort, reject) => {
    const probe = createServer();
    probe.once('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const address = probe.address();
      if (!address || typeof address === 'string') {
        probe.close();
        reject(new Error('Could not allocate Team dev-server port'));
        return;
      }
      probe.close((error) => {
        if (error) reject(error);
        else resolvePort(address.port);
      });
    });
  });
}

async function fetchWhenReady(url, processOutput) {
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return response.text();
    } catch {
      // The child has not bound the port yet.
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 100));
  }
  throw new Error(`Team dev server did not become ready:\n${processOutput()}`);
}

function requireText(value, expected, label) {
  if (!value.includes(expected)) {
    throw new Error(`${label} is missing ${expected}`);
  }
}

const port = await availablePort();
const output = [];
const child = spawn(
  process.platform === 'win32' ? 'npm.cmd' : 'npm',
  ['run', 'dev', '--', '--host', '127.0.0.1', '--port', String(port), '--strictPort'],
  {
    cwd: webRoot,
    env: { ...process.env, FRISKET_EDITION: 'team' },
    detached: process.platform !== 'win32',
    stdio: ['ignore', 'pipe', 'pipe'],
  },
);
child.stdout.on('data', (chunk) => output.push(String(chunk)));
child.stderr.on('data', (chunk) => output.push(String(chunk)));

try {
  const origin = `http://127.0.0.1:${port}`;
  const html = await fetchWhenReady(`${origin}/`, () => output.join(''));
  requireText(html, '/src/entries/team.tsx', 'Team dev HTML');
  if (html.includes('/src/entries/local.tsx')) {
    throw new Error('Team dev HTML selected the Local entry');
  }

  const entry = await fetchWhenReady(
    `${origin}/src/entries/team.tsx`,
    () => output.join(''),
  );
  requireText(entry, 'TEAM_EDITION_MODULE', 'Team dev entry');
  requireText(entry, 'mountEdition', 'Team dev entry');
  requireText(entry, 'frisket-edition:team', 'Team dev entry');
  if (entry.includes('LOCAL_EDITION_MODULE')) {
    throw new Error('Team dev entry imported the Local edition module');
  }
  console.log('Team dev entry: /src/entries/team.tsx -> TEAM_EDITION_MODULE');
} finally {
  if (process.platform === 'win32' || child.pid === undefined) {
    child.kill('SIGTERM');
  } else {
    try {
      process.kill(-child.pid, 'SIGTERM');
    } catch (error) {
      if (error.code !== 'ESRCH') throw error;
    }
  }
  await Promise.race([
    new Promise((resolveExit) => child.once('exit', resolveExit)),
    new Promise((resolveWait) => setTimeout(resolveWait, 2_000)),
  ]);
}
