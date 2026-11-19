// A second local stack whose backend runs under FRISKET_CACHE_MODE=replay,
// for the specs that strict replay cannot express: strict replay with a cache
// proves no model call can spend, so it never raises the cost gate, and it
// refuses every uncached call, including one to a local endpoint. The suite's
// own stack keeps the mode it was started with; only specs that import `test`
// from this module boot and use this one.
//
// Under plain replay a cache miss goes live, so the backend gets a proxy on a
// closed loopback port for every scheme, exempting loopback only: provider
// hosts are unreachable from it, while a local endpoint on 127.0.0.1 is not.

import { execFileSync, spawn, type ChildProcess } from 'node:child_process';
import { rmSync } from 'node:fs';
import path from 'node:path';
import { test as base } from '@playwright/test';

import { dynamicPortPair, newIsolatedE2EWorkspace } from '../../playwright/playwright.local-stack';
import { returningUserStorageState } from '../../playwright/playwright.returning-user';
import { testPython } from '../support/testPython';

const WEB_DIR = path.basename(process.cwd()) === 'web' ? process.cwd() : path.join(process.cwd(), 'web');
const REPO_ROOT = path.resolve(WEB_DIR, '..');
const CLOSED_PROXY = 'http://127.0.0.1:9';
const LOOPBACK = '127.0.0.1,localhost,::1';
const BOOT_TIMEOUT_MS = 90_000;

type ReplayStack = {
  frontendURL: string;
  workspace: string;
};

function startProcess(name: string, command: string, env: NodeJS.ProcessEnv): ChildProcess {
  const child = spawn('bash', ['-c', command], {
    cwd: WEB_DIR,
    env,
    detached: true,
    stdio: ['ignore', 'inherit', 'inherit'],
  });
  child.on('error', (error) => console.error(`[replay-stack] ${name}: ${error.message}`));
  return child;
}

function stopProcess(child: ChildProcess): void {
  if (child.pid === undefined || child.exitCode !== null) return;
  try {
    process.kill(-child.pid, 'SIGTERM');
  } catch {
    // The process group already exited.
  }
}

async function waitForOk(url: string, children: ChildProcess[]): Promise<void> {
  const deadline = Date.now() + BOOT_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const exited = children.find((child) => child.exitCode !== null);
    if (exited) throw new Error(`replay stack process exited with ${exited.exitCode} before ${url} answered`);
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {
      // Not listening yet.
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`replay stack did not answer ${url} within ${BOOT_TIMEOUT_MS}ms`);
}

/** Write a placeholder provider key into one project of the replay stack's
 *  workspace (scripts/e2e/seed_provider_key.py). */
export function seedPlaceholderProviderKey(stack: ReplayStack, pid: string, provider: string): void {
  execFileSync(testPython(), ['scripts/e2e/seed_provider_key.py', stack.workspace, pid, provider], {
    cwd: REPO_ROOT,
    stdio: 'pipe',
    timeout: 60_000,
  });
}

type ReplayStackFixtures = {
  replayCacheMode: 'replay';
  replayStack: ReplayStack;
};

export const test = base.extend<object, ReplayStackFixtures>({
  replayCacheMode: ['replay', { scope: 'worker', option: true }],
  replayStack: [async ({ replayCacheMode }, provide) => {
    const { frontendPort, backendPort } = dynamicPortPair();
    const frontendURL = `http://127.0.0.1:${frontendPort}`;
    const backendURL = `http://127.0.0.1:${backendPort}`;
    const workspace = newIsolatedE2EWorkspace('replay');
    const backend = startProcess('backend', 'bash ../scripts/e2e/e2e_backend.sh', {
      ...process.env,
      FRISKET_E2E_WS: workspace,
      FRISKET_E2E_BASE_WS: workspace,
      FRISKET_E2E_BACKEND_PORT: String(backendPort),
      FRISKET_E2E_ISOLATED: '0',
      FRISKET_CACHE_MODE: replayCacheMode,
      HTTP_PROXY: CLOSED_PROXY,
      HTTPS_PROXY: CLOSED_PROXY,
      ALL_PROXY: CLOSED_PROXY,
      http_proxy: CLOSED_PROXY,
      https_proxy: CLOSED_PROXY,
      all_proxy: CLOSED_PROXY,
      NO_PROXY: LOOPBACK,
      no_proxy: LOOPBACK,
    });
    const frontend = startProcess(
      'frontend',
      `npm run dev -- --host 127.0.0.1 --port ${frontendPort} --strictPort`,
      { ...process.env, FRISKET_EDITION: 'local', FRISKET_BACKEND_URL: backendURL },
    );
    try {
      await waitForOk(`${backendURL}/api/health`, [backend, frontend]);
      await waitForOk(frontendURL, [backend, frontend]);
      await provide({ frontendURL, workspace });
    } finally {
      stopProcess(frontend);
      stopProcess(backend);
      rmSync(workspace, { recursive: true, force: true });
    }
  }, { scope: 'worker', timeout: BOOT_TIMEOUT_MS * 2 }],
  baseURL: async ({ replayStack }, provide) => {
    await provide(replayStack.frontendURL);
  },
  storageState: async ({ replayStack }, provide) => {
    await provide(returningUserStorageState(replayStack.frontendURL));
  },
});

export { expect } from '@playwright/test';
