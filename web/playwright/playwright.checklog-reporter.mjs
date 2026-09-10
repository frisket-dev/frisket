import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { cleanupCurrentE2EWorkspace } from '../e2e-workspace-cleanup.mjs';
import {
  createE2EDurationTracker,
  durationSummaryPath,
  formatDurationSummary,
  safeDiagnosticText,
} from '../e2e-duration-diagnostics.mjs';

const CONFIG_DIR = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(CONFIG_DIR, '../..');
const DEFAULT_LOG = path.join(ROOT, '.frisket', 'check-runs.jsonl');

function isoNow() {
  return new Date().toISOString();
}

function checklogPath() {
  return process.env.FRISKET_CHECKLOG_PATH || DEFAULT_LOG;
}

function disabled() {
  return process.env.FRISKET_CHECKLOG_DISABLE === '1';
}

function appendRecord(record) {
  if (disabled()) return;
  const target = checklogPath();
  try {
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.appendFileSync(target, `${JSON.stringify(record)}\n`, 'utf8');
  } catch (error) {
    console.error(`[checklog] unable to write ${target}: ${error.message}`);
  }
}

function durationDiagnosticsDisabled() {
  return process.env.FRISKET_E2E_DURATION_DISABLE === '1';
}

function commandArgLabel(arg) {
  const safe = safeDiagnosticText(arg, 240);
  if (!safe || safe.includes('[redacted-')) return safe;
  const normalized = safe.replace(/\\/g, '/');
  if (!normalized.includes('/')) return safe;
  const absolute = path.isAbsolute(safe) ? safe : path.resolve(process.cwd(), safe);
  const relative = path.relative(ROOT, absolute).replace(/\\/g, '/');
  if (relative && !relative.startsWith('..') && !path.isAbsolute(relative)) {
    return safeDiagnosticText(relative, 240);
  }
  return safeDiagnosticText(path.basename(normalized), 240);
}

function commandLabel() {
  const args = process.argv.slice(2).filter((arg) => arg !== 'test');
  return args.length ? safeDiagnosticText(args.map(commandArgLabel).join(' '), 240) : 'playwright';
}

function commandArgs() {
  return process.argv.slice(1).map(commandArgLabel);
}

function cwdLabel() {
  const relative = path.relative(ROOT, process.cwd()).replace(/\\/g, '/');
  if (!relative) return '.';
  if (!relative.startsWith('..') && !path.isAbsolute(relative)) return relative;
  return safeDiagnosticText(process.cwd(), 240);
}

export default class ChecklogReporter {
  constructor() {
    this.startedAt = isoNow();
    this.started = Date.now();
    this.counts = new Map();
    this.total = 0;
    this.durationTracker = durationDiagnosticsDisabled()
      ? null
      : createE2EDurationTracker();
  }

  onBegin(config, suite) {
    this.startedAt = isoNow();
    this.started = Date.now();
    this.total = suite.allTests().length;
    this.durationTracker?.onBegin(config, suite);
  }

  onTestBegin(test) {
    this.durationTracker?.onTestBegin(test);
  }

  onTestEnd(test, result) {
    this.counts.set(result.status, (this.counts.get(result.status) || 0) + 1);
    this.durationTracker?.onTestEnd(test, result);
  }

  onEnd(result) {
    const finishedAt = isoNow();
    const elapsedSeconds = Math.max(0, (Date.now() - this.started) / 1000);
    const outcome = result.status === 'passed' ? 'PASS' : 'FAIL';
    const cleanupStarted = Date.now();
    const cleanup = cleanupCurrentE2EWorkspace({ status: result.status });
    const cleanupElapsedSeconds = Math.max(0, (Date.now() - cleanupStarted) / 1000);
    const durationSummary = this.durationTracker?.onEnd(result, {
      cleanup: { ...cleanup, elapsed_seconds: cleanupElapsedSeconds },
    });
    appendRecord({
      schema_version: 1,
      source: 'playwright',
      kind: 'playwright',
      label: commandLabel(),
      command: commandArgs(),
      cwd: cwdLabel(),
      outcome,
      returncode: outcome === 'PASS' ? 0 : 1,
      elapsed_seconds: Number(elapsedSeconds.toFixed(3)),
      started_at: this.startedAt,
      finished_at: finishedAt,
      metadata: {
        status: result.status,
        tests_total: this.total,
        test_statuses: Object.fromEntries(this.counts),
        duration_summary_path: this.durationTracker
          ? commandArgLabel(durationSummaryPath())
          : undefined,
        duration_phases: durationSummary?.phases,
      },
    });
    if (durationSummary) {
      console.error(`[e2e-duration]\n${formatDurationSummary(durationSummary)}`);
    }
  }
}
