import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import ChecklogReporter from '../playwright/playwright.checklog-reporter.mjs';
import {
  appendDurationEvent,
  createE2EDurationTracker,
  formatDurationSummary,
} from '../e2e-duration-diagnostics.mjs';

function tempRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'frisket-duration-test-'));
}

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

async function freePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const address = server.address();
  const port = typeof address === 'object' && address ? address.port : 0;
  await new Promise((resolve) => server.close(resolve));
  return port;
}

async function waitFor(predicate, message) {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
  }
  throw new Error(message);
}

function fakeTest({ id, projectName, file, title }) {
  return {
    id,
    location: { file },
    title,
    project: () => ({ name: projectName }),
    titlePath: () => [projectName, title],
  };
}

test('duration events persist only safe allowlisted fields', () => {
  const root = tempRoot();
  const eventsPath = path.join(root, 'events.jsonl');
  appendDurationEvent(
    {
      phase: 'backend_boot',
      name: 'backend http://127.0.0.1:8000/health?token=SECRET 127.0.0.1:9000 sk-secret-token',
      elapsed_seconds: 1.23456,
      stdout: 'do not store',
      stderr: 'token=SECRET',
      project_id: 'project-secret',
      url: 'http://127.0.0.1:8000/api/health?token=SECRET',
    },
    { path: eventsPath, now: () => new Date('2026-06-18T00:00:00Z') },
  );

  const raw = fs.readFileSync(eventsPath, 'utf8');
  const event = JSON.parse(raw);
  assert.deepEqual(Object.keys(event).sort(), [
    'elapsed_seconds',
    'name',
    'phase',
    'run_id',
    'schema_version',
    'source',
    'timestamp',
  ]);
  assert.equal(event.elapsed_seconds, 1.235);
  assert.equal(raw.includes('SECRET'), false);
  assert.equal(raw.includes('sk-secret-token'), false);
  assert.equal(raw.includes('project-secret'), false);
  assert.equal(raw.includes('127.0.0.1'), false);
  fs.rmSync(root, { recursive: true, force: true });
});

test('tracker groups backend boot, frontend boot, seed, ui action, and teardown', () => {
  const root = tempRoot();
  const eventsPath = path.join(root, 'events.jsonl');
  appendDurationEvent(
    { phase: 'backend_boot', name: 'backend', elapsed_seconds: 1.4 },
    { path: eventsPath },
  );
  appendDurationEvent(
    { phase: 'frontend_boot', name: 'frontend', elapsed_seconds: 0.8 },
    { path: eventsPath },
  );

  const seed = fakeTest({
    id: 'seed-1',
    projectName: 'seed',
    file: '/repo/web/tests/e2e/seed.setup.ts',
    title: 'seed demo projects (once per workspace)',
  });
  const body = fakeTest({
    id: 'body-1',
    projectName: 'chromium',
    file: '/repo/web/tests/e2e/admin-debug-errors.spec.ts',
    title: 'shows recent errors without docker logs',
  });
  const tracker = createE2EDurationTracker({
    env: {
      FRISKET_E2E_DURATION_EVENTS_PATH: eventsPath,
      FRISKET_E2E_DURATION_SUMMARY_PATH: path.join(root, 'summary.json'),
      FRISKET_E2E_CONFIGURED_AT_MS: String(Date.parse('2026-06-18T00:00:00Z')),
    },
    argv: [
      'node',
      'playwright',
      'test',
      '--grep',
      'http://127.0.0.1:8000/p/demo?token=SECRET sk-secret-token',
      'admin-debug-errors.spec.ts',
    ],
    now: () => new Date('2026-06-18T00:00:02Z'),
  });

  tracker.onBegin({ workers: 2 }, { allTests: () => [seed, body] });
  tracker.onTestEnd(seed, { status: 'passed', duration: 1250 });
  tracker.onTestEnd(body, { status: 'failed', duration: 3250 });
  const summary = tracker.onEnd(
    { status: 'failed' },
    { cleanup: { removed: true, elapsed_seconds: 0.42 } },
  );

  assert.equal(summary.schema_version, 1);
  assert.equal(summary.source, 'playwright');
  assert.equal(summary.outcome, 'FAIL');
  assert.equal(summary.tests_total, 2);
  assert.equal(summary.phases.backend_boot.elapsed_seconds, 1.4);
  assert.equal(summary.phases.frontend_boot.elapsed_seconds, 0.8);
  assert.equal(summary.phases.setup.elapsed_seconds, 0);
  assert.equal(summary.phases.seed.elapsed_seconds, 1.25);
  assert.equal(summary.phases.ui_action.elapsed_seconds, 3.25);
  assert.equal(summary.phases.teardown.elapsed_seconds, 0.42);
  assert.deepEqual(summary.slowest_tests, [
    {
      duration_seconds: 3.25,
      file: 'web/tests/e2e/admin-debug-errors.spec.ts',
      project: 'chromium',
      status: 'failed',
      title: 'shows recent errors without docker logs',
    },
    {
      duration_seconds: 1.25,
      file: 'web/tests/e2e/seed.setup.ts',
      project: 'seed',
      status: 'passed',
      title: 'seed demo projects (once per workspace)',
    },
  ]);
  const serialized = JSON.stringify(summary);
  assert.equal(serialized.includes('stdout'), false);
  assert.equal(serialized.includes('stderr'), false);
  assert.equal(serialized.includes('SECRET'), false);
  assert.equal(serialized.includes('sk-secret-token'), false);
  assert.equal(serialized.includes('127.0.0.1'), false);
  assert.equal(fs.existsSync(path.join(root, 'summary.json')), true);
  fs.rmSync(root, { recursive: true, force: true });
});

test('tracker ignores redirected events from other e2e runs', () => {
  const root = tempRoot();
  const eventsPath = path.join(root, 'events.jsonl');
  appendDurationEvent(
    { phase: 'backend_boot', name: 'backend', elapsed_seconds: 99, run_id: 'old-run' },
    { path: eventsPath },
  );
  appendDurationEvent(
    { phase: 'backend_boot', name: 'backend', elapsed_seconds: 2.5, run_id: 'new-run' },
    { path: eventsPath },
  );
  const tracker = createE2EDurationTracker({
    env: {
      FRISKET_E2E_RUN_ID: 'new-run',
      FRISKET_E2E_DURATION_EVENTS_PATH: eventsPath,
      FRISKET_E2E_DURATION_SUMMARY_PATH: path.join(root, 'summary.json'),
    },
  });

  tracker.onBegin({ workers: 1 }, { allTests: () => [] });
  const summary = tracker.onEnd({ status: 'passed' });

  assert.equal(summary.phases.backend_boot.elapsed_seconds, 2.5);
  fs.rmSync(root, { recursive: true, force: true });
});

test('diagnostic write failures stay best-effort', () => {
  const root = tempRoot();
  const blocker = path.join(root, 'not-a-directory');
  fs.writeFileSync(blocker, 'occupied');
  const eventsPath = path.join(blocker, 'events.jsonl');
  const summaryPath = path.join(blocker, 'summary.json');
  const errors = [];
  const previousError = console.error;
  console.error = (...args) => {
    errors.push(args.join(' '));
  };

  try {
    assert.doesNotThrow(() => {
      appendDurationEvent(
        { phase: 'backend_boot', name: 'backend', elapsed_seconds: 1.25 },
        { path: eventsPath },
      );
    });

    const tracker = createE2EDurationTracker({
      env: {
        FRISKET_E2E_DURATION_EVENTS_PATH: eventsPath,
        FRISKET_E2E_DURATION_SUMMARY_PATH: summaryPath,
      },
    });
    tracker.onBegin({ workers: 1 }, { allTests: () => [] });
    let summary = null;
    assert.doesNotThrow(() => {
      summary = tracker.onEnd({ status: 'passed' });
    });
    assert.equal(summary.status, 'passed');
    assert.equal(fs.existsSync(summaryPath), false);
    assert.equal(errors.join('\n').includes(root), false);
  } finally {
    console.error = previousError;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('formatted summary gives a concise next slow-check move', () => {
  const text = formatDurationSummary({
    phases: {
      backend_boot: { elapsed_seconds: 1.4, count: 1 },
      frontend_boot: { elapsed_seconds: 0.8, count: 1 },
      seed: { elapsed_seconds: 1.25, count: 1 },
      ui_action: { elapsed_seconds: 3.25, count: 1 },
      teardown: { elapsed_seconds: 0.42, count: 1 },
    },
    slowest_tests: [
      {
        duration_seconds: 3.25,
        file: 'web/tests/e2e/admin-debug-errors.spec.ts',
        project: 'chromium',
        status: 'failed',
        title: 'shows recent errors without docker logs',
      },
    ],
  });

  assert.match(text, /Slowest phase: ui action \(3\.25s\)/);
  assert.match(text, /Next: inspect chromium web\/tests\/e2e\/admin-debug-errors\.spec\.ts/);
  assert.equal(text.includes('stdout'), false);
});

test('server wrapper records boot readiness without command logs', async () => {
  const root = tempRoot();
  const eventsPath = path.join(root, 'events.jsonl');
  const port = await freePort();
  const serverScript = path.join(root, 'server.mjs');
  fs.writeFileSync(
    serverScript,
    [
      "import http from 'node:http';",
      "if (process.env.WRAPPED_PHASE !== 'backend') process.exit(23);",
      "setTimeout(() => {",
      "  http.createServer((_req, res) => { res.end('token=SECRET'); })",
      `    .listen(${port}, '127.0.0.1');`,
      "}, 100);",
      'setInterval(() => {}, 1000);',
    ].join('\n'),
  );
  const proc = spawn(
    process.execPath,
    [
      'web/playwright/e2e-duration-server.mjs',
      '--phase',
      'backend_boot',
      '--name',
      'backend',
      '--url',
      `http://127.0.0.1:${port}/health?token=SECRET`,
      '--timeout',
      '5000',
      '--',
      'WRAPPED_PHASE=backend',
      process.execPath,
      serverScript,
    ],
    {
      cwd: ROOT,
      env: { ...process.env, FRISKET_E2E_DURATION_EVENTS_PATH: eventsPath },
      stdio: ['ignore', 'pipe', 'pipe'],
    },
  );
  try {
    await waitFor(() => fs.existsSync(eventsPath), 'duration wrapper did not write event');
    const raw = fs.readFileSync(eventsPath, 'utf8');
    const event = JSON.parse(raw);
    assert.equal(event.phase, 'backend_boot');
    assert.equal(event.name, 'backend');
    assert.equal(typeof event.elapsed_seconds, 'number');
    assert.equal(raw.includes('SECRET'), false);
    assert.equal(raw.includes('/health'), false);
  } finally {
    proc.kill('SIGTERM');
    await new Promise((resolve) => proc.once('exit', resolve));
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('server wrapper fails fast when child exits before readiness', async () => {
  const root = tempRoot();
  const eventsPath = path.join(root, 'events.jsonl');
  const port = await freePort();
  const started = Date.now();
  const proc = spawn(
    process.execPath,
    [
      'web/playwright/e2e-duration-server.mjs',
      '--phase',
      'backend_boot',
      '--name',
      'backend',
      '--url',
      `http://127.0.0.1:${port}/health`,
      '--timeout',
      '5000',
      '--',
      process.execPath,
      '-e',
      'process.exit(23)',
    ],
    {
      cwd: ROOT,
      env: { ...process.env, FRISKET_E2E_DURATION_EVENTS_PATH: eventsPath },
      stdio: ['ignore', 'pipe', 'pipe'],
    },
  );
  const stderr = [];
  proc.stderr.on('data', (chunk) => stderr.push(String(chunk)));
  const code = await new Promise((resolve) => proc.once('exit', resolve));

  assert.equal(code, 1);
  assert.ok(Date.now() - started < 2000);
  assert.match(stderr.join(''), /exited before readiness/);
  assert.equal(fs.existsSync(eventsPath), false);
  fs.rmSync(root, { recursive: true, force: true });
});

test('server wrapper times out live child without hanging or leaking readiness URL', async () => {
  const root = tempRoot();
  const eventsPath = path.join(root, 'events.jsonl');
  const port = await freePort();
  const proc = spawn(
    process.execPath,
    [
      'web/playwright/e2e-duration-server.mjs',
      '--phase',
      'backend_boot',
      '--name',
      'backend',
      '--url',
      `http://127.0.0.1:${port}/health?token=SECRET`,
      '--timeout',
      '500',
      '--',
      process.execPath,
      '-e',
      'setInterval(() => {}, 1000)',
    ],
    {
      cwd: ROOT,
      env: { ...process.env, FRISKET_E2E_DURATION_EVENTS_PATH: eventsPath },
      stdio: ['ignore', 'pipe', 'pipe'],
    },
  );
  const stderr = [];
  proc.stderr.on('data', (chunk) => stderr.push(String(chunk)));
  const started = Date.now();
  const code = await new Promise((resolve) => proc.once('exit', resolve));
  const output = stderr.join('');

  assert.equal(code, 1);
  assert.ok(Date.now() - started < 2500);
  assert.match(output, /Timed out waiting/);
  assert.equal(output.includes('SECRET'), false);
  assert.equal(output.includes('127.0.0.1'), false);
  assert.equal(fs.existsSync(eventsPath), false);
  fs.rmSync(root, { recursive: true, force: true });
});

test('server wrapper escalates timeout cleanup for children that ignore sigterm', async () => {
  const root = tempRoot();
  const eventsPath = path.join(root, 'events.jsonl');
  const port = await freePort();
  const childScript = path.join(root, 'ignore-term.mjs');
  fs.writeFileSync(
    childScript,
    [
      "process.on('SIGTERM', () => {});",
      'setInterval(() => {}, 1000);',
    ].join('\n'),
  );
  const proc = spawn(
    process.execPath,
    [
      'web/playwright/e2e-duration-server.mjs',
      '--phase',
      'backend_boot',
      '--name',
      'backend',
      '--url',
      `http://127.0.0.1:${port}/health?token=SECRET`,
      '--timeout',
      '500',
      '--',
      process.execPath,
      childScript,
    ],
    {
      cwd: ROOT,
      env: { ...process.env, FRISKET_E2E_DURATION_EVENTS_PATH: eventsPath },
      stdio: ['ignore', 'pipe', 'pipe'],
    },
  );
  const stderr = [];
  proc.stderr.on('data', (chunk) => stderr.push(String(chunk)));
  const started = Date.now();
  const code = await new Promise((resolve) => proc.once('exit', resolve));
  const output = stderr.join('');

  assert.equal(code, 1);
  assert.ok(Date.now() - started < 3500);
  assert.match(output, /Timed out waiting/);
  assert.equal(output.includes('SECRET'), false);
  assert.equal(output.includes('127.0.0.1'), false);
  assert.equal(fs.existsSync(eventsPath), false);
  fs.rmSync(root, { recursive: true, force: true });
});

test('server wrapper honors duration disable flag', async () => {
  const root = tempRoot();
  const eventsPath = path.join(root, 'events.jsonl');
  const port = await freePort();
  const serverScript = path.join(root, 'server.mjs');
  fs.writeFileSync(
    serverScript,
    [
      "import http from 'node:http';",
      "http.createServer((_req, res) => { res.end('ready'); })",
      `  .listen(${port}, '127.0.0.1');`,
      'setInterval(() => {}, 1000);',
    ].join('\n'),
  );
  const proc = spawn(
    process.execPath,
    [
      'web/playwright/e2e-duration-server.mjs',
      '--phase',
      'frontend_boot',
      '--name',
      'frontend',
      '--url',
      `http://127.0.0.1:${port}/`,
      '--timeout',
      '5000',
      '--',
      process.execPath,
      serverScript,
    ],
    {
      cwd: ROOT,
      env: {
        ...process.env,
        FRISKET_E2E_DURATION_DISABLE: '1',
        FRISKET_E2E_DURATION_EVENTS_PATH: eventsPath,
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    },
  );
  try {
    await new Promise((resolve) => setTimeout(resolve, 500));
    assert.equal(fs.existsSync(eventsPath), false);
  } finally {
    proc.kill('SIGTERM');
    await new Promise((resolve) => proc.once('exit', resolve));
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('checklog reporter writes duration summary metadata', () => {
  const root = tempRoot();
  const previous = {
    checklog: process.env.FRISKET_CHECKLOG_PATH,
    checklogDisable: process.env.FRISKET_CHECKLOG_DISABLE,
    events: process.env.FRISKET_E2E_DURATION_EVENTS_PATH,
    summary: process.env.FRISKET_E2E_DURATION_SUMMARY_PATH,
    disable: process.env.FRISKET_E2E_DURATION_DISABLE,
    isolated: process.env.FRISKET_E2E_ISOLATED,
    workspace: process.env.FRISKET_E2E_WS,
    argv: process.argv,
    cwd: process.cwd(),
  };
  const checklogPath = path.join(root, 'check-runs.jsonl');
  const eventsPath = path.join(root, 'events.jsonl');
  const summaryPath = path.join(root, 'duration-summary-token=SECRET.json');
  process.env.FRISKET_CHECKLOG_PATH = checklogPath;
  delete process.env.FRISKET_CHECKLOG_DISABLE;
  process.env.FRISKET_E2E_DURATION_EVENTS_PATH = eventsPath;
  process.env.FRISKET_E2E_DURATION_SUMMARY_PATH = summaryPath;
  delete process.env.FRISKET_E2E_DURATION_DISABLE;
  delete process.env.FRISKET_E2E_ISOLATED;
  delete process.env.FRISKET_E2E_WS;
    process.argv = [
      process.execPath,
      '/Users/example/secret-home/node_modules/.bin/playwright',
      'test',
      '--grep',
      '127.0.0.1:8000/p/demo?token=SECRET sk-secret-token',
      'grid-sort.spec.ts',
    ];
  process.chdir(ROOT);

  try {
    appendDurationEvent(
      { phase: 'frontend_boot', name: 'frontend', elapsed_seconds: 0.7 },
      { path: eventsPath },
    );
    const body = fakeTest({
      id: 'body-1',
      projectName: 'chromium',
      file: '/repo/web/tests/e2e/grid-sort.spec.ts',
      title: 'sorts rows',
    });
    const reporter = new ChecklogReporter();
    reporter.onBegin({ workers: 1 }, { allTests: () => [body] });
    reporter.onTestBegin(body);
    reporter.onTestEnd(body, { status: 'passed', duration: 900 });
    reporter.onEnd({ status: 'passed' });

    const checklog = JSON.parse(fs.readFileSync(checklogPath, 'utf8'));
    const summary = JSON.parse(fs.readFileSync(summaryPath, 'utf8'));
    assert.equal(checklog.metadata.duration_summary_path, 'duration-summary-token=[redacted]');
    assert.equal(checklog.metadata.duration_phases.frontend_boot.elapsed_seconds, 0.7);
    assert.equal(summary.phases.ui_action.elapsed_seconds, 0.9);
    const serialized = JSON.stringify({ checklog, summary });
    assert.equal(serialized.includes('stdout'), false);
    assert.equal(serialized.includes('SECRET'), false);
    assert.equal(serialized.includes('sk-secret-token'), false);
    assert.equal(serialized.includes('127.0.0.1'), false);
    assert.equal(serialized.includes('/Users/example/secret-home'), false);
    assert.equal(checklog.command[0], 'playwright');
    assert.equal(checklog.cwd, '.');
  } finally {
    const envNames = {
      checklog: 'FRISKET_CHECKLOG_PATH',
      checklogDisable: 'FRISKET_CHECKLOG_DISABLE',
      events: 'FRISKET_E2E_DURATION_EVENTS_PATH',
      summary: 'FRISKET_E2E_DURATION_SUMMARY_PATH',
      disable: 'FRISKET_E2E_DURATION_DISABLE',
      isolated: 'FRISKET_E2E_ISOLATED',
      workspace: 'FRISKET_E2E_WS',
    };
    for (const [key, envName] of Object.entries(envNames)) {
      const value = previous[key];
      if (value === undefined) {
        delete process.env[envName];
      } else {
        process.env[envName] = value;
      }
    }
    process.argv = previous.argv;
    process.chdir(previous.cwd);
    fs.rmSync(root, { recursive: true, force: true });
  }
});
