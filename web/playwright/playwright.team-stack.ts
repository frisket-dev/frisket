import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { mkdirSync } from 'node:fs';
import {
  durationWrappedServerCommand,
  dynamicPortPair,
  intEnv,
  shellQuote,
} from './playwright.local-stack';

// process.cwd(), NOT import.meta.url: this module and the team config can be
// bundled into one temporary file, at which point import.meta.url resolves to
// the bundle rather than this source file. Both normal and bundled invocations
// run with cwd=web/, so cwd is the reliable anchor.
const WEB_DIR = process.cwd();
const ROOT = path.resolve(WEB_DIR, '..');

/**
 * Self-booting SINGLE-ORG TEAM stack for specs that only exist under
 * `capabilities.team`/`capabilities.identity`. Boots the
 * real open single-org ASGI entrypoint (`frisket.team.asgi:app`, the same
 * module `uvicorn` serves in the shipped team image — see
 * scripts/build_release_artifacts.py) as the backend, and `vite dev` with
 * `FRISKET_EDITION=team` as the frontend — mirroring playwright.local-stack.ts
 * but for the team edition instead of the local single-user edition.
 *
 * `FRISKET_EDITION=team` selects the Team entry HTML. That entry passes the
 * immutable `TEAM_EDITION_MODULE` at mount, which unlocks the Team-gated
 * routes and settings through the same context path every composition uses.
 */
function teamPortPair(): { frontendPort: number; backendPort: number } {
  const explicitFrontend = intEnv('FRISKET_E2E_TEAM_FRONTEND_PORT');
  const explicitBackend = intEnv('FRISKET_E2E_TEAM_BACKEND_PORT');
  if (explicitFrontend && explicitBackend) {
    // A worker-process reload of the SAME run (Playwright re-imports the
    // config file per worker; teamStack() runs again in a fresh process).
    // The first load already probed, locked, and persisted these — a second
    // dynamicPortPair() call here would either re-probe a DIFFERENT pair
    // (breaking the already-booted webServer) or deadlock re-acquiring a
    // lock this same run already holds.
    return { frontendPort: explicitFrontend, backendPort: explicitBackend };
  }
  const pair = dynamicPortPair(explicitFrontend, explicitBackend);
  process.env.FRISKET_E2E_TEAM_FRONTEND_PORT = String(pair.frontendPort);
  process.env.FRISKET_E2E_TEAM_BACKEND_PORT = String(pair.backendPort);
  return pair;
}

export function teamStack() {
  const { frontendPort, backendPort } = teamPortPair();
  const frontendURL = `http://127.0.0.1:${frontendPort}`;
  const backendURL = `http://127.0.0.1:${backendPort}`;

  // Persisted (not just read) so a worker-process reload of this module
  // resolves the SAME run workspace instead of a fresh random one — the
  // team-seed setup project and the team-admin test project must agree on
  // exactly one storageStatePath.
  const runId = process.env.FRISKET_E2E_RUN_ID ?? `${process.pid}-${randomUUID()}`;
  process.env.FRISKET_E2E_RUN_ID = runId;
  const runRoot =
    process.env.FRISKET_E2E_WS ?? path.join(os.tmpdir(), `frisket-team-e2e-${runId}`);
  process.env.FRISKET_E2E_WS = runRoot;
  mkdirSync(runRoot, { recursive: true });

  const dataDir = process.env.FRISKET_DATA_DIR ?? path.join(runRoot, 'team-data');
  const databaseUrl =
    process.env.FRISKET_TEAM_DATABASE_URL ?? `sqlite:///${path.join(runRoot, 'team.db')}`;
  const runQueueDatabaseUrl =
    process.env.FRISKET_RUN_QUEUE_DATABASE_URL ??
    `sqlite:///${path.join(runRoot, 'team-queue.db')}`;
  const secretsKeyFile =
    process.env.FRISKET_SECRETS_KEY_FILE ?? path.join(runRoot, 'team-secrets.key');
  const organizationName = process.env.FRISKET_ORGANIZATION_NAME ?? 'Acceptance Team';
  const adminEmails = process.env.FRISKET_ADMIN_EMAILS ?? 'owner@example.test';
  const smtpHost = process.env.FRISKET_SMTP_HOST ?? '127.0.0.1';
  const smtpFrom = process.env.FRISKET_SMTP_FROM ?? 'team@example.test';
  const smtpStarttls = process.env.FRISKET_SMTP_STARTTLS ?? 'false';
  const storageStatePath = path.join(runRoot, 'team-admin-storage-state.json');
  const reportPath = path.join(ROOT, '.frisket', 'team-e2e-report.json');

  // Persist the resolved values back onto process.env: playwright inherits
  // this env when it spawns the setup/test worker processes below, exactly
  // like playwright.local-stack.ts's localStack() does for
  // FRISKET_E2E_BACKEND_URL (consumed downstream by tests/e2e/seed.setup.ts).
  process.env.FRISKET_EDITION = 'team';
  process.env.FRISKET_TEAM_DATABASE_URL = databaseUrl;
  process.env.FRISKET_RUN_QUEUE_DATABASE_URL = runQueueDatabaseUrl;
  process.env.FRISKET_DATA_DIR = dataDir;
  process.env.FRISKET_SECRETS_KEY_FILE = secretsKeyFile;
  process.env.FRISKET_ORGANIZATION_NAME = organizationName;
  process.env.FRISKET_ADMIN_EMAILS = adminEmails;
  process.env.FRISKET_TEAM_BACKEND_URL = backendURL;
  process.env.FRISKET_TEAM_STORAGE_STATE = storageStatePath;

  const backendCommand =
    `FRISKET_TEAM_DATABASE_URL=${shellQuote(databaseUrl)} ` +
    `FRISKET_RUN_QUEUE_DATABASE_URL=${shellQuote(runQueueDatabaseUrl)} ` +
    `FRISKET_DATA_DIR=${shellQuote(dataDir)} ` +
    `FRISKET_BASE_URL=${shellQuote(backendURL)} ` +
    `FRISKET_ORGANIZATION_NAME=${shellQuote(organizationName)} ` +
    `FRISKET_ADMIN_EMAILS=${shellQuote(adminEmails)} ` +
    `FRISKET_SMTP_HOST=${shellQuote(smtpHost)} ` +
    `FRISKET_SMTP_FROM=${shellQuote(smtpFrom)} ` +
    `FRISKET_SMTP_STARTTLS=${shellQuote(smtpStarttls)} ` +
    `FRISKET_SECRETS_KEY_FILE=${shellQuote(secretsKeyFile)} ` +
    // The real production entrypoint (frisket.team.asgi:app), the same
    // module uvicorn serves for the shipped team image. `--project` (not a
    // shell `cd`) resolves the repo venv regardless of the launcher's cwd —
    // the duration wrapper spawns this argv directly, with no shell to
    // interpret `&&`/`cd`, and a probe may launch this command with cwd=web.
    // --extra entities: followthemoney used to be a base dependency;
    // keep it here so this e2e stack preserves the FollowTheMoney/graph-
    // neighborhood/frisket.ftm runtime surface it had before, not a
    // silently narrower one.
    `uv run --extra entities --project ${shellQuote(ROOT)} uvicorn frisket.team.asgi:app ` +
    `--host 127.0.0.1 --port ${backendPort}`;

  const frontendCommand =
    `FRISKET_EDITION=team FRISKET_BACKEND_URL=${shellQuote(backendURL)} ` +
    `npm run dev -- --host 127.0.0.1 --port ${frontendPort} --strictPort`;

  return {
    baseURL: frontendURL,
    backendURL,
    frontendURL,
    dataDir,
    databaseUrl,
    storageStatePath,
    reportPath,
    webServer: [
      {
        command: durationWrappedServerCommand({
          phase: 'team_backend_boot',
          name: 'team-backend',
          url: `${backendURL}/api/health`,
          timeoutMs: 60_000,
          command: backendCommand,
        }),
        cwd: WEB_DIR,
        url: `${backendURL}/api/health`,
        reuseExistingServer: false,
        timeout: 60_000,
      },
      {
        command: durationWrappedServerCommand({
          phase: 'team_frontend_boot',
          name: 'team-frontend',
          url: frontendURL,
          timeoutMs: 30_000,
          command: frontendCommand,
        }),
        cwd: WEB_DIR,
        url: frontendURL,
        reuseExistingServer: false,
        timeout: 30_000,
      },
    ],
  };
}
