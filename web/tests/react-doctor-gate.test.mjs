// Disposition-trail contract for the react-doctor gate: the score is telemetry only,
// never a threshold; what gates is (a) zero ERROR-tier findings, (b) every
// current warning already accounted for (baselined or doctor.config.ts
// override), (c) improvement (fewer warnings than baseline) always passes,
// reported informationally rather than failing.
//
// Drives web/scripts/react-doctor-gate-lib.mjs against fixture
// diagnostics/baseline/config JSON written to a temp dir — no real
// react-doctor run.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  computeGateReport,
  loadBaselineFromFile,
  loadDoctorConfigOverrides,
} from '../scripts/react-doctor-gate-lib.mjs';

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'frisket-doctor-gate-test-'));
}

function writeBaseline(dir, pairs) {
  const baselinePath = path.join(dir, 'baseline.json');
  fs.writeFileSync(baselinePath, JSON.stringify({ warnings: pairs }, null, 2));
  return baselinePath;
}

function writeDoctorConfig(dir, overridesSource) {
  const configPath = path.join(dir, 'doctor.config.ts');
  fs.writeFileSync(
    configPath,
    `export default {\n  ignore: {\n    overrides: [\n${overridesSource}\n    ],\n  },\n};\n`,
  );
  return configPath;
}

function warningDiagnostic({ rule, file, plugin = 'react-doctor', line = 1 }) {
  return {
    filePath: file,
    plugin,
    rule,
    severity: 'warning',
    title: `fixture ${rule}`,
    message: 'fixture message',
    line,
    category: 'Maintainability',
  };
}

function errorDiagnostic({ rule, file, plugin = 'react-doctor', line = 1 }) {
  return {
    filePath: file,
    plugin,
    rule,
    severity: 'error',
    title: `fixture ${rule}`,
    message: 'fixture message',
    line,
    category: 'Bugs',
  };
}

const BASELINE_PAIR = { rule: 'react-doctor/prefer-useReducer', file: 'src/components/Foo.tsx' };
const EMPTY_OVERRIDES = [];

test('a warning not in the baseline and not covered by an override fails the gate', () => {
  const dir = tempDir();
  const baselinePath = writeBaseline(dir, [BASELINE_PAIR]);
  const baseline = loadBaselineFromFile(baselinePath);

  const diagnostics = [
    warningDiagnostic({ rule: 'prefer-useReducer', file: 'src/components/Foo.tsx' }),
    warningDiagnostic({ rule: 'no-giant-component', file: 'src/components/Bar.tsx' }),
  ];

  const report = computeGateReport({ diagnostics, baseline, overrides: EMPTY_OVERRIDES });

  assert.equal(report.ok, false);
  assert.equal(report.newWarnings.length, 1);
  assert.deepEqual(report.newWarnings[0], {
    rule: 'react-doctor/no-giant-component',
    file: 'src/components/Bar.tsx',
  });
  assert.equal(report.errorFindings.length, 0);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('a warning already in the baseline passes the gate', () => {
  const dir = tempDir();
  const baselinePath = writeBaseline(dir, [BASELINE_PAIR]);
  const baseline = loadBaselineFromFile(baselinePath);

  const diagnostics = [warningDiagnostic({ rule: 'prefer-useReducer', file: 'src/components/Foo.tsx' })];

  const report = computeGateReport({ diagnostics, baseline, overrides: EMPTY_OVERRIDES });

  assert.equal(report.ok, true);
  assert.equal(report.newWarnings.length, 0);
  assert.equal(report.disappearedWarnings.length, 0);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('a warning covered by a doctor.config.ts override passes even though it is not baselined', () => {
  const dir = tempDir();
  const baselinePath = writeBaseline(dir, []);
  const baseline = loadBaselineFromFile(baselinePath);
  const configPath = writeDoctorConfig(
    dir,
    "      { files: ['src/components/Overridden.tsx'], rules: ['react-doctor/no-giant-component'] },",
  );
  const overrides = loadDoctorConfigOverrides(configPath);

  const diagnostics = [warningDiagnostic({ rule: 'no-giant-component', file: 'src/components/Overridden.tsx' })];

  const report = computeGateReport({ diagnostics, baseline, overrides });

  assert.equal(report.ok, true);
  assert.equal(report.newWarnings.length, 0);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('any ERROR-tier finding fails the gate regardless of baseline or overrides', () => {
  const dir = tempDir();
  const baselinePath = writeBaseline(dir, [BASELINE_PAIR]);
  const baseline = loadBaselineFromFile(baselinePath);

  const diagnostics = [
    warningDiagnostic({ rule: 'prefer-useReducer', file: 'src/components/Foo.tsx' }),
    errorDiagnostic({ rule: 'no-eval', file: 'src/components/Dangerous.tsx' }),
  ];

  const report = computeGateReport({ diagnostics, baseline, overrides: EMPTY_OVERRIDES });

  assert.equal(report.ok, false);
  assert.equal(report.errorFindings.length, 1);
  assert.equal(report.errorFindings[0].rule, 'no-eval');
  // The baselined warning alongside it must not itself cause a failure —
  // only the error-tier finding does.
  assert.equal(report.newWarnings.length, 0);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('fewer warnings than the baseline (improvement) passes and reports the disappearance', () => {
  const dir = tempDir();
  const baselinePath = writeBaseline(dir, [
    BASELINE_PAIR,
    { rule: 'react-doctor/no-giant-component', file: 'src/components/Fixed.tsx' },
  ]);
  const baseline = loadBaselineFromFile(baselinePath);

  // "Fixed.tsx" no longer shows up in the doctor's diagnostics at all.
  const diagnostics = [warningDiagnostic({ rule: 'prefer-useReducer', file: 'src/components/Foo.tsx' })];

  const report = computeGateReport({ diagnostics, baseline, overrides: EMPTY_OVERRIDES });

  assert.equal(report.ok, true);
  assert.equal(report.newWarnings.length, 0);
  assert.equal(report.disappearedWarnings.length, 1);
  assert.deepEqual(report.disappearedWarnings[0], {
    rule: 'react-doctor/no-giant-component',
    file: 'src/components/Fixed.tsx',
  });
  fs.rmSync(dir, { recursive: true, force: true });
});
