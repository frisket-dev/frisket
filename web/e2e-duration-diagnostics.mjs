import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const WEB_DIR = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(WEB_DIR, '..');
const DEFAULT_DIR = path.join(ROOT, '.frisket');
const DEFAULT_EVENTS = path.join(DEFAULT_DIR, 'e2e-duration-events.jsonl');
const DEFAULT_SUMMARY = path.join(DEFAULT_DIR, 'e2e-duration-summary.json');
const PHASES = ['setup', 'backend_boot', 'frontend_boot', 'seed', 'ui_action', 'teardown'];

function nowDate(now) {
  const value = typeof now === 'function' ? now() : now;
  return value instanceof Date ? value : new Date(value ?? Date.now());
}

function nowMs(now) {
  return nowDate(now).getTime();
}

function roundSeconds(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric < 0) return 0;
  return Number(numeric.toFixed(3));
}

export function safeDiagnosticText(value, max = 160) {
  let text = String(value ?? '');
  text = text.replace(/https?:\/\/[^\s)"']+/g, '[redacted-url]');
  text = text.replace(
    /\b(?:localhost|(?:\d{1,3}\.){3}\d{1,3})(?::\d{1,5})?(?:\/[^\s)"']*)?/gi,
    '[redacted-host]',
  );
  text = text.replace(
    /\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?::\d{1,5}|\/[^\s)"']*)[^\s)"']*/g,
    '[redacted-url]',
  );
  text = text.replace(
    /((?:api[_-]?key|token|secret|password|session)\s*[:=]\s*)[^\s,;&)"']+/gi,
    '$1[redacted]',
  );
  text = text.replace(/\bsk-[A-Za-z0-9_-]{8,}\b/g, '[redacted-key]');
  text = text.replace(/\bfrisket_pat_[A-Za-z0-9_-]{8,}\b/g, '[redacted-token]');
  text = text.replace(
    /(^|[^A-Za-z0-9_=-])([A-Za-z0-9_=-]{40,})(?=$|[^A-Za-z0-9_=-])/g,
    '$1[redacted-token]',
  );
  return text.replace(/\s+/g, ' ').trim().slice(0, max);
}

function safePhase(value) {
  return PHASES.includes(value) ? value : 'setup';
}

function writeErrorReason(error) {
  if (error && typeof error === 'object' && typeof error.code === 'string') {
    return safeDiagnosticText(error.code, 40);
  }
  return error instanceof Error ? safeDiagnosticText(error.name, 40) : 'unknown';
}

function relativeFile(file) {
  const normalized = String(file || '').replace(/\\/g, '/');
  const webTests = normalized.lastIndexOf('/web/tests/');
  if (webTests >= 0) return normalized.slice(webTests + 1);
  const webRoot = normalized.lastIndexOf('/web/');
  if (webRoot >= 0) return normalized.slice(webRoot + 1);
  if (!normalized) return '';
  const absolute = path.resolve(normalized);
  const relative = path.relative(ROOT, absolute).replace(/\\/g, '/');
  if (relative && !relative.startsWith('..') && !path.isAbsolute(relative)) return relative;
  return path.basename(normalized);
}

function projectName(test) {
  try {
    return safeDiagnosticText(
      test.project?.()?.name || test.parent?.project?.()?.name || test.projectName || 'unknown',
      80,
    );
  } catch {
    return safeDiagnosticText(test.projectName || 'unknown', 80);
  }
}

function titleFor(test) {
  if (typeof test.title === 'string' && test.title) return safeDiagnosticText(test.title, 180);
  if (typeof test.titlePath === 'function') {
    const parts = test.titlePath().filter(Boolean);
    return safeDiagnosticText(String(parts[parts.length - 1] || ''), 180);
  }
  return 'unknown';
}

function phaseForTest(test) {
  const project = projectName(test);
  const file = relativeFile(test.location?.file || '');
  if (project === 'seed' || file.endsWith('seed.setup.ts')) return 'seed';
  return 'ui_action';
}

function emptyPhases() {
  return Object.fromEntries(
    PHASES.map((phase) => [phase, { count: 0, elapsed_seconds: 0 }]),
  );
}

function addPhase(phases, phase, elapsedSeconds, count = 1) {
  const key = safePhase(phase);
  const bucket = phases[key] || { count: 0, elapsed_seconds: 0 };
  bucket.count += count;
  bucket.elapsed_seconds = roundSeconds(bucket.elapsed_seconds + roundSeconds(elapsedSeconds));
  phases[key] = bucket;
}

function phaseLabel(phase) {
  return safePhase(phase).replace(/_/g, ' ');
}

function durationEventsPath(env = process.env) {
  return env.FRISKET_E2E_DURATION_EVENTS_PATH || DEFAULT_EVENTS;
}

export function durationSummaryPath(env = process.env) {
  return env.FRISKET_E2E_DURATION_SUMMARY_PATH || DEFAULT_SUMMARY;
}

function normalizeDurationEvent(
  event,
  { now = () => new Date(), env = process.env } = {},
) {
  return {
    schema_version: 1,
    source: 'playwright',
    run_id: safeDiagnosticText(event?.run_id || env.FRISKET_E2E_RUN_ID || 'unknown', 120),
    phase: safePhase(event?.phase),
    name: safeDiagnosticText(event?.name || event?.phase || 'phase', 80),
    elapsed_seconds: roundSeconds(event?.elapsed_seconds),
    timestamp: nowDate(now).toISOString(),
  };
}

export function appendDurationEvent(event, options = {}) {
  const target = options.path || durationEventsPath(options.env);
  if (!target) return;
  const normalized = normalizeDurationEvent(event, options);
  try {
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.appendFileSync(target, `${JSON.stringify(normalized)}\n`, 'utf8');
  } catch (error) {
    console.error(
      `[e2e-duration] unable to write duration event: ${writeErrorReason(error)}`,
    );
  }
}

function readEvents(target) {
  try {
    if (!target || !fs.existsSync(target)) return [];
  } catch {
    return [];
  }
  const out = [];
  let content = '';
  try {
    content = fs.readFileSync(target, 'utf8');
  } catch {
    return [];
  }
  for (const line of content.split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const parsed = JSON.parse(line);
      out.push({
        run_id: safeDiagnosticText(parsed.run_id || 'unknown', 120),
        phase: safePhase(parsed.phase),
        name: safeDiagnosticText(parsed.name || parsed.phase || 'phase', 80),
        elapsed_seconds: roundSeconds(parsed.elapsed_seconds),
      });
    } catch {
      // Ignore malformed timing lines; diagnostics must never break e2e cleanup.
    }
  }
  return out;
}

function commandLabel(argv) {
  const args = (argv || []).slice(2).filter((arg) => arg !== 'test');
  return args.length ? safeDiagnosticText(args.join(' '), 240) : 'playwright';
}

function statusOutcome(status) {
  return status === 'passed' ? 'PASS' : 'FAIL';
}

export function createE2EDurationTracker(options = {}) {
  const env = options.env || process.env;
  const argv = options.argv || process.argv;
  const now = options.now || (() => new Date());
  const eventsPath = durationEventsPath(env);
  const summaryPath = durationSummaryPath(env);
  const runId = safeDiagnosticText(env.FRISKET_E2E_RUN_ID || 'unknown', 120);
  const phases = emptyPhases();
  const testStarts = new Map();
  const slowestTests = [];
  let startedAt = nowDate(now).toISOString();
  let startedMs = nowMs(now);
  let testsTotal = 0;
  let workers = null;
  let externalEventsRead = false;

  function trackSlowTest(test, status, durationSeconds) {
    slowestTests.push({
      duration_seconds: roundSeconds(durationSeconds),
      file: relativeFile(test.location?.file || ''),
      project: projectName(test),
      status: safeDiagnosticText(status || 'unknown', 40),
      title: titleFor(test),
    });
    slowestTests.sort((a, b) => b.duration_seconds - a.duration_seconds);
    slowestTests.splice(5);
  }

  function summaryFor(result = {}) {
    if (!externalEventsRead) {
      externalEventsRead = true;
      const externalEvents = readEvents(eventsPath);
      let explicitBootSeconds = 0;
      for (const event of externalEvents) {
        if (event.run_id && event.run_id !== runId) continue;
        addPhase(phases, event.phase, event.elapsed_seconds);
        if (event.phase === 'backend_boot' || event.phase === 'frontend_boot') {
          explicitBootSeconds += event.elapsed_seconds;
        }
      }
      if (explicitBootSeconds > 0 && phases.setup) {
        phases.setup.elapsed_seconds = roundSeconds(
          Math.max(0, phases.setup.elapsed_seconds - explicitBootSeconds),
        );
      }
    }
    const finishedAt = nowDate(now).toISOString();
    const elapsedSeconds = roundSeconds((nowMs(now) - startedMs) / 1000);
    return {
      schema_version: 1,
      source: 'playwright',
      label: commandLabel(argv),
      outcome: statusOutcome(result.status),
      status: safeDiagnosticText(result.status || 'unknown', 40),
      tests_total: testsTotal,
      workers,
      elapsed_seconds: elapsedSeconds,
      started_at: startedAt,
      finished_at: finishedAt,
      phases,
      slowest_tests: slowestTests,
    };
  }

  return {
    onBegin(config = {}, suite = { allTests: () => [] }) {
      startedAt = nowDate(now).toISOString();
      startedMs = nowMs(now);
      workers = Number.isFinite(Number(config.workers)) ? Number(config.workers) : null;
      testsTotal =
        typeof suite.allTests === 'function' ? Number(suite.allTests().length) : 0;
      const configuredAt = Number(env.FRISKET_E2E_CONFIGURED_AT_MS || 0);
      if (Number.isFinite(configuredAt) && configuredAt > 0 && configuredAt <= startedMs) {
        addPhase(phases, 'setup', (startedMs - configuredAt) / 1000);
      }
    },
    onTestBegin(test) {
      if (test?.id) testStarts.set(test.id, nowMs(now));
    },
    onTestEnd(test, result = {}) {
      const durationSeconds = Number.isFinite(Number(result.duration))
        ? Number(result.duration) / 1000
        : (nowMs(now) - (testStarts.get(test?.id) || startedMs)) / 1000;
      const phase = phaseForTest(test || {});
      addPhase(phases, phase, durationSeconds);
      trackSlowTest(test || {}, result.status, durationSeconds);
    },
    onEnd(result = {}, details = {}) {
      if (details.cleanup && Number.isFinite(Number(details.cleanup.elapsed_seconds))) {
        addPhase(phases, 'teardown', Number(details.cleanup.elapsed_seconds));
      }
      const summary = summaryFor(result);
      try {
        fs.mkdirSync(path.dirname(summaryPath), { recursive: true });
        fs.writeFileSync(summaryPath, `${JSON.stringify(summary, null, 2)}\n`, 'utf8');
      } catch (error) {
        console.error(
          `[e2e-duration] unable to write duration summary: ${writeErrorReason(error)}`,
        );
      }
      return summary;
    },
    summary(result = {}) {
      return summaryFor(result);
    },
  };
}

export function formatDurationSummary(summary) {
  const phases = summary?.phases || {};
  const entries = Object.entries(phases)
    .filter(([, value]) => Number(value?.elapsed_seconds || 0) > 0)
    .sort((a, b) => Number(b[1].elapsed_seconds) - Number(a[1].elapsed_seconds));
  if (!entries.length) return 'No Playwright duration diagnostics recorded.';
  const [phase, value] = entries[0];
  const label = phaseLabel(phase);
  const lines = [`Slowest phase: ${label} (${roundSeconds(value.elapsed_seconds)}s)`];
  const slowest = summary?.slowest_tests?.[0];
  if (slowest) {
    lines.push(
      `Next: inspect ${slowest.project} ${slowest.file} (${roundSeconds(
        slowest.duration_seconds,
      )}s)`,
    );
  }
  return lines.join('\n');
}
