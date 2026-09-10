#!/usr/bin/env node
// react-doctor DISPOSITION-TRAIL gate. Metrics are smells, not targets — this
// gate does NOT threshold the react-doctor score. It gates on the
// disposition trail instead:
//   (a) ERROR-tier findings are always 0 — hard fail, no baseline covers them.
//   (b) every CURRENT warning must already be accounted for: either in the
//       committed web/.react-doctor-warning-baseline.json, or covered by a
//       doctor.config.ts ignore override. A brand-new (rule, file) pair
//       fails the gate — fix it or justify it in doctor.config.ts with a
//       written reason.
//   (c) warnings that disappeared from the baseline are reported so the
//       baseline can be re-shrunk deliberately, but improvement never fails
//       the gate.
// The remote score is printed as telemetry only — clearly labeled
// non-gating — alongside warning-count deltas.
//
// Exit codes: 0 clean, 1 gate violation (error-tier finding or new
// unaccounted warning).

import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { computeGateReport, loadBaselineFromFile, loadDoctorConfigOverrides } from './react-doctor-gate-lib.mjs';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(scriptDir, '..');
const baselinePath = resolve(webRoot, '.react-doctor-warning-baseline.json');
const doctorConfigPath = resolve(webRoot, 'doctor.config.ts');
const diagnosticsPath = resolve(webRoot, '.react-doctor', 'diagnostics.json');

function runDoctor() {
  const binName = process.platform === 'win32' ? 'react-doctor.cmd' : 'react-doctor';
  const reactDoctor = resolve(webRoot, 'node_modules', '.bin', binName);
  return spawnSync(
    reactDoctor,
    ['.', '--yes', '--blocking', 'none', '--output-dir', '.react-doctor', '--score', '--no-color'],
    { cwd: webRoot, encoding: 'utf8' },
  );
}

function parseScore(output) {
  const scoreMatch =
    output.match(/\b(\d{1,3})\s*\/\s*100\b/) ??
    output.match(/\b(?:react doctor )?score[:\s]+(\d{1,3})\b/i) ??
    output.match(/^\s*(\d{1,3})\s*$/m);
  if (scoreMatch?.[1]) return Number.parseInt(scoreMatch[1], 10);

  const numbers = [...output.matchAll(/\b(\d{1,3})\b/g)].map((match) => match[1]);
  const uniqueNumbers = [...new Set(numbers)];
  if (uniqueNumbers.length === 1) return Number.parseInt(uniqueNumbers[0], 10);
  return null;
}

function formatPair(pair) {
  return `${pair.rule} @ ${pair.file}`;
}

function main() {
  const proc = runDoctor();
  if (proc.error) {
    console.error(proc.error.message);
    process.exit(1);
  }
  const output = `${proc.stdout || ''}\n${proc.stderr || ''}`.trim();
  if (proc.status !== 0) {
    if (output) console.error(output);
    process.exit(proc.status ?? 1);
  }

  let diagnostics;
  try {
    diagnostics = JSON.parse(readFileSync(diagnosticsPath, 'utf8'));
  } catch (error) {
    console.error(`Could not read React Doctor diagnostics at ${diagnosticsPath}: ${error.message}`);
    process.exit(1);
  }
  if (!Array.isArray(diagnostics)) {
    console.error(`React Doctor diagnostics must be an array: ${diagnosticsPath}`);
    process.exit(1);
  }

  let baseline;
  try {
    baseline = loadBaselineFromFile(baselinePath);
  } catch (error) {
    console.error(`Could not read warning baseline at ${baselinePath}: ${error.message}`);
    process.exit(1);
  }

  let overrides;
  try {
    overrides = loadDoctorConfigOverrides(doctorConfigPath);
  } catch (error) {
    console.error(`Could not read doctor.config.ts overrides at ${doctorConfigPath}: ${error.message}`);
    process.exit(1);
  }

  const report = computeGateReport({ diagnostics, baseline, overrides });

  const score = parseScore(output);
  console.log(
    `React Doctor score: ${score ?? 'unknown'}/100 — TELEMETRY only, non-gating (metrics are smells, not targets).`,
  );

  console.log(
    `Warning counts: baseline ${report.baselineCount} -> current ${report.currentCount} ` +
      `(new ${report.newWarnings.length}, disappeared ${report.disappearedWarnings.length}).`,
  );

  if (report.disappearedWarnings.length > 0) {
    console.log(
      `Disappeared from baseline (informational — re-shrink the baseline deliberately if these are gone for good):`,
    );
    for (const pair of report.disappearedWarnings) {
      console.log(`  - ${formatPair(pair)}`);
    }
  }

  let failed = false;

  if (report.errorFindings.length > 0) {
    failed = true;
    console.error(`\nReact Doctor gate: ${report.errorFindings.length} ERROR-tier finding(s) — hard fail:`);
    for (const diagnostic of report.errorFindings) {
      const location = `${diagnostic.filePath ?? 'unknown'}:${diagnostic.line ?? '?'}`;
      console.error(
        `  - ${diagnostic.severity ?? 'unknown'} ${diagnostic.plugin ?? '?'}/${diagnostic.rule ?? 'unknown'} at ${location}: ${diagnostic.title ?? ''}`,
      );
    }
  }

  if (report.newWarnings.length > 0) {
    failed = true;
    console.error(
      `\nReact Doctor gate: ${report.newWarnings.length} new warning(s) not in the baseline and not covered ` +
        `by a doctor.config.ts override — fix it or justify it in doctor.config.ts with a written reason:`,
    );
    for (const pair of report.newWarnings) {
      console.error(`  - ${formatPair(pair)}`);
    }
  }

  if (failed) {
    process.exit(1);
  }

  console.log('\nReact Doctor gate: clean (0 error-tier findings, 0 unaccounted new warnings).');
}

main();
