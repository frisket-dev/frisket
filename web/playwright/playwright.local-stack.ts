import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { mkdirSync, readdirSync, rmSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  prepareE2EWorkspace,
  pruneStaleBaseWorkspaceProjects,
  pruneStaleE2EWorkspaces,
  removeActiveMarker,
} from '../e2e-workspace-cleanup.mjs';

export const shellQuote = (value: string) => `'${value.replace(/'/g, "'\\''")}'`;
const CONFIG_DIR = path.dirname(fileURLToPath(import.meta.url));
const WEB_DIR = path.resolve(CONFIG_DIR, '..');
const ROOT = path.resolve(CONFIG_DIR, '../..');
const acquiredPortLocks: string[] = [];
let cleanupRegistered = false;

// Exported so other self-booting stacks (e.g. playwright.team-stack.ts) can
// wrap their own webServer commands in the same readiness/duration wrapper
// instead of re-implementing it.
export function durationWrappedServerCommand({
  phase,
  name,
  url,
  timeoutMs,
  command,
}: {
  phase: string;
  name: string;
  url: string;
  timeoutMs: number;
  command: string;
}): string {
  return (
    `node playwright/e2e-duration-server.mjs --phase ${shellQuote(phase)} ` +
    `--name ${shellQuote(name)} --url ${shellQuote(url)} --timeout ${timeoutMs} -- ` +
    command
  );
}

export function intEnv(name: string): number | undefined {
  const raw = process.env[name];
  if (!raw) return undefined;
  const parsed = Number(raw);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : undefined;
}

function cleanupPortLocks() {
  for (const lockPath of acquiredPortLocks.splice(0)) {
    rmSync(lockPath, { recursive: true, force: true });
  }
}

function registerCleanup() {
  if (cleanupRegistered) return;
  cleanupRegistered = true;
  process.once('exit', cleanupPortLocks);
  process.once('SIGINT', () => {
    cleanupPortLocks();
    process.exit(130);
  });
  process.once('SIGTERM', () => {
    cleanupPortLocks();
    process.exit(143);
  });
}

function pruneStalePortLocks(lockRoot: string) {
  const maxAgeMs = 24 * 60 * 60 * 1000;
  mkdirSync(lockRoot, { recursive: true });
  for (const name of readdirSync(lockRoot)) {
    const lockPath = path.join(lockRoot, name);
    try {
      const stat = statSync(lockPath);
      if (Date.now() - stat.mtimeMs > maxAgeMs) {
        rmSync(lockPath, { recursive: true, force: true });
      }
    } catch {
      // Ignore races with another test process removing its own lock.
    }
  }
}

function acquirePortPairLock(frontendPort: number, backendPort: number): boolean {
  const lockRoot = path.join(os.tmpdir(), 'frisket-e2e-port-locks');
  pruneStalePortLocks(lockRoot);
  const lockPath = path.join(lockRoot, `${frontendPort}-${backendPort}.lock`);
  try {
    mkdirSync(lockPath);
    acquiredPortLocks.push(lockPath);
    registerCleanup();
    return true;
  } catch {
    return false;
  }
}

function probeFreePortPair(excludedPorts: number[]): { frontendPort: number; backendPort: number } {
  const probeScript = String.raw`
const net = require('node:net');
const excluded = new Set(process.argv.slice(1).map((value) => Number.parseInt(value, 10)).filter(Number.isInteger));
const servers = [];
function reserve() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      const port = typeof address === 'object' && address ? address.port : 0;
      if (!port || excluded.has(port)) {
        server.close(() => reserve().then(resolve, reject));
        return;
      }
      excluded.add(port);
      servers.push(server);
      resolve(port);
    });
  });
}
(async () => {
  const frontendPort = await reserve();
  const backendPort = await reserve();
  console.log(JSON.stringify({ frontendPort, backendPort }));
  await Promise.all(servers.map((server) => new Promise((resolve) => server.close(resolve))));
})().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
`;
  const proc = spawnSync(process.execPath, ['-e', probeScript, ...excludedPorts.map(String)], {
    encoding: 'utf8',
  });
  if (proc.status !== 0) {
    throw new Error(`Unable to allocate Playwright e2e ports:\n${proc.stderr || proc.stdout}`);
  }
  return JSON.parse(proc.stdout) as { frontendPort: number; backendPort: number };
}

// Exported so other self-booting stacks can reserve their own OS-assigned,
// cross-process-locked port pair without re-implementing port probing/locking.
export function dynamicPortPair(
  explicitFrontend?: number,
  explicitBackend?: number,
): { frontendPort: number; backendPort: number } {
  const excluded = [explicitFrontend, explicitBackend].filter((value): value is number => value !== undefined);
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const probed = probeFreePortPair(excluded);
    const frontendPort = explicitFrontend ?? probed.frontendPort;
    const backendPort = explicitBackend ?? probed.backendPort;
    if (frontendPort === backendPort) continue;
    if (acquirePortPairLock(frontendPort, backendPort)) {
      return { frontendPort, backendPort };
    }
  }
  throw new Error('Unable to lock a unique Playwright e2e port pair after 20 attempts');
}

function localPortPair(): { frontendPort: number; backendPort: number; fixedPorts: boolean } {
  const fixedPorts = process.env.FRISKET_E2E_FIXED_PORTS === '1';
  const explicitFrontend = intEnv('FRISKET_E2E_FRONTEND_PORT');
  const explicitBackend = intEnv('FRISKET_E2E_BACKEND_PORT');
  const inheritedPortLock = process.env.FRISKET_E2E_PORT_LOCK_HELD === '1';

  if (!fixedPorts) {
    if (explicitFrontend && explicitBackend) {
      if (
        !inheritedPortLock &&
        !acquirePortPairLock(explicitFrontend, explicitBackend)
      ) {
        throw new Error(
          `Explicit Playwright e2e port pair ${explicitFrontend}/${explicitBackend} is already locked by another run`,
        );
      }
      return { frontendPort: explicitFrontend, backendPort: explicitBackend, fixedPorts };
    }
    return { ...dynamicPortPair(explicitFrontend, explicitBackend), fixedPorts };
  }

  return {
    frontendPort: explicitFrontend ?? 5173,
    backendPort: explicitBackend ?? 8000,
    fixedPorts,
  };
}

export function localStack() {
  const { frontendPort, backendPort, fixedPorts } = localPortPair();
  const frontendURL = `http://127.0.0.1:${frontendPort}`;
  const backendURL = `http://127.0.0.1:${backendPort}`;
  const baseWorkspace =
    process.env.FRISKET_E2E_BASE_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const runId = process.env.FRISKET_E2E_RUN_ID ?? `${process.pid}-${randomUUID()}`;
  const workspace =
    process.env.FRISKET_E2E_WS ??
    (fixedPorts ? baseWorkspace : path.join(os.tmpdir(), `frisket-e2e-${runId}`));

  // The local config excludes team-only specs, so its frontend posture must
  // not depend on a caller's inherited shell environment.
  process.env.FRISKET_EDITION = 'local';
  process.env.FRISKET_E2E_BASE_URL = process.env.FRISKET_E2E_BASE_URL ?? frontendURL;
  process.env.FRISKET_E2E_BACKEND_URL = backendURL;
  process.env.FRISKET_E2E_FRONTEND_PORT = String(frontendPort);
  process.env.FRISKET_E2E_BACKEND_PORT = String(backendPort);
  if (!fixedPorts) process.env.FRISKET_E2E_PORT_LOCK_HELD = '1';
  process.env.FRISKET_E2E_BASE_WS = baseWorkspace;
  process.env.FRISKET_E2E_WS = workspace;
  process.env.FRISKET_E2E_RUN_ID = runId;
  process.env.FRISKET_E2E_ISOLATED = fixedPorts ? '0' : '1';
  process.env.FRISKET_E2E_CONFIGURED_AT_MS = process.env.FRISKET_E2E_CONFIGURED_AT_MS ?? String(Date.now());
  process.env.FRISKET_E2E_DURATION_EVENTS_PATH =
    process.env.FRISKET_E2E_DURATION_EVENTS_PATH ??
    path.join(ROOT, '.frisket', `e2e-duration-events-${runId}.jsonl`);
  process.env.FRISKET_E2E_DURATION_SUMMARY_PATH =
    process.env.FRISKET_E2E_DURATION_SUMMARY_PATH ??
    path.join(ROOT, '.frisket', `e2e-duration-summary-${runId}.json`);
  pruneStaleE2EWorkspaces({ currentWorkspace: workspace });
  // Keep the persistent base workspace from accumulating leaked per-run project
  // dirs (the isolated-workspace prune above never touches the fixed base).
  pruneStaleBaseWorkspaceProjects({ workspace: baseWorkspace, runId });
  prepareE2EWorkspace({ workspace, runId });
  process.once('exit', () => removeActiveMarker(workspace));

  const backendCommand =
    `FRISKET_E2E_WS=${shellQuote(workspace)} ` +
    `FRISKET_E2E_BASE_WS=${shellQuote(baseWorkspace)} ` +
    `FRISKET_E2E_BACKEND_PORT=${backendPort} ` +
    `FRISKET_E2E_ISOLATED=${process.env.FRISKET_E2E_ISOLATED} ` +
    'FRISKET_ENABLE_REAL_LOCAL_PLUGIN_SMOKE_HANDLER=1 ' +
    'bash ../scripts/e2e/e2e_backend.sh';
  const frontendCommand =
    `FRISKET_EDITION=local FRISKET_BACKEND_URL=${shellQuote(backendURL)} ` +
    `npm run dev -- --host 127.0.0.1 --port ${frontendPort} --strictPort`;

  return {
    baseURL: process.env.FRISKET_E2E_BASE_URL,
    backendURL,
    backendHealthURL: `${backendURL}/api/health`,
    frontendURL,
    fixedPorts,
    webServer: [
      {
        command: durationWrappedServerCommand({
          phase: 'backend_boot',
          name: 'backend',
          url: `${backendURL}/api/health`,
          timeoutMs: 60_000,
          command: backendCommand,
        }),
        cwd: WEB_DIR,
        url: `${backendURL}/api/health`,
        reuseExistingServer: fixedPorts,
        timeout: 60_000,
      },
      {
        command: durationWrappedServerCommand({
          phase: 'frontend_boot',
          name: 'frontend',
          url: frontendURL,
          timeoutMs: 30_000,
          command: frontendCommand,
        }),
        cwd: WEB_DIR,
        url: frontendURL,
        reuseExistingServer: fixedPorts,
        timeout: 30_000,
      },
    ],
  };
}
