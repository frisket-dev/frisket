#!/usr/bin/env node
// Regenerates React Doctor diagnostics fresh (never trusts a stale
// .react-doctor/diagnostics.json) and asserts a given file or directory has
// zero findings, optionally scoped to one rule. This is the one honest gate
// every doctor mint proves itself with; doctor:changed is advisory-only
// (--blocking none) and can never fail a check.cmd on its own.
//
// Usage:
//   node assert-doctor-clean.mjs <file-or-dir> [rule]
//   node assert-doctor-clean.mjs --self-test
import assert from 'node:assert';
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(scriptDir, '..');

// diagnostics.json's filePath is web/-relative (e.g. "src/AnswersView.tsx"),
// but callers pass repo-root-relative paths (e.g. "web/src/AnswersView.tsx")
// to match the other check commands. Strip the leading
// "web/" so the two line up.
function normalizePathArg(pathArg) {
  let normalized = pathArg.replace(/\\/g, '/');
  if (normalized.startsWith('./')) normalized = normalized.slice(2);
  if (normalized === 'web') normalized = '';
  else if (normalized.startsWith('web/')) normalized = normalized.slice('web/'.length);
  normalized = normalized.replace(/\/+$/, '');
  return normalized;
}

// Boundary-aware prefix match: "src/core/commands" must match
// "src/core/commands/foo.ts" and "src/core/commands" itself, but must NOT
// match a sibling like "src/core/commands-legacy/foo.ts".
function matchesPathPrefix(filePath, prefix) {
  if (typeof filePath !== 'string') return false;
  if (prefix === '') return true;
  return filePath === prefix || filePath.startsWith(`${prefix}/`);
}

function filterFindings(diagnostics, pathPrefix, rule) {
  return diagnostics.filter((diagnostic) => {
    if (!matchesPathPrefix(diagnostic?.filePath, pathPrefix)) return false;
    if (rule && diagnostic?.rule !== rule) return false;
    return true;
  });
}

function formatFinding(finding) {
  const location = `${finding.filePath ?? 'unknown'}:${finding.line ?? '?'}`;
  return `${location} ${finding.rule ?? 'unknown-rule'}`;
}

function regenerateDiagnostics() {
  const binName = process.platform === 'win32' ? 'react-doctor.cmd' : 'react-doctor';
  const reactDoctor = resolve(webRoot, 'node_modules', '.bin', binName);

  // Same spawnSync invocation as check-react-doctor-score.mjs: run fresh,
  // never trust a stale diagnostics.json, land it in the same output dir.
  const proc = spawnSync(
    reactDoctor,
    ['.', '--yes', '--blocking', 'none', '--output-dir', '.react-doctor', '--score', '--no-color'],
    { cwd: webRoot, encoding: 'utf8' },
  );

  if (proc.error) {
    console.error(proc.error.message);
    process.exit(1);
  }

  const output = `${proc.stdout || ''}\n${proc.stderr || ''}`.trim();
  if (proc.status !== 0) {
    if (output) console.error(output);
    process.exit(proc.status ?? 1);
  }

  const diagnosticsPath = resolve(webRoot, '.react-doctor', 'diagnostics.json');
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

  return diagnostics;
}

const SELF_TEST_FIXTURE = [
  {
    filePath: 'src/workbench/AnswersView.tsx',
    rule: 'no-adjust-state-on-prop-change',
    severity: 'error',
    line: 238,
  },
  {
    filePath: 'src/workbench/AnswersView.tsx',
    rule: 'no-giant-component',
    severity: 'warning',
    line: 89,
  },
  {
    filePath: 'src/components/ModelPicker.tsx',
    rule: 'unused-export',
    severity: 'warning',
    line: 12,
  },
  // Deliberate near-miss sibling to prove the prefix match is
  // boundary-aware, not a bare string startsWith.
  {
    filePath: 'src/components-legacy/Old.tsx',
    rule: 'unused-export',
    severity: 'warning',
    line: 1,
  },
];

function runSelfTestCase({ name, pathArg, rule, expectFindings }) {
  const pathPrefix = normalizePathArg(pathArg);
  const findings = filterFindings(SELF_TEST_FIXTURE, pathPrefix, rule);
  const gotFindings = findings.length > 0;
  const pass = gotFindings === expectFindings;
  const direction = expectFindings ? 'must fail (findings present)' : 'must pass (no findings)';
  console.log(`  ${pass ? 'PASS' : 'FAIL'} — ${name}: ${direction}, got ${findings.length} finding(s)`);
  if (!pass) {
    findings.forEach((f) => console.log(`       ${formatFinding(f)}`));
  }
  return pass;
}

function runSelfTest() {
  console.log('assert-doctor-clean.mjs --self-test: filtering logic against a fixture payload\n');

  const cases = [
    {
      name: 'file + rule match (fails direction)',
      pathArg: 'web/src/workbench/AnswersView.tsx',
      rule: 'no-adjust-state-on-prop-change',
      expectFindings: true,
    },
    {
      name: 'file match, no rule filter (fails direction)',
      pathArg: 'web/src/workbench/AnswersView.tsx',
      expectFindings: true,
    },
    {
      name: 'directory prefix match (fails direction)',
      pathArg: 'web/src/components',
      expectFindings: true,
    },
    {
      name: 'file exists but rule does not fire there (passes direction)',
      pathArg: 'web/src/workbench/AnswersView.tsx',
      rule: 'unused-export',
      expectFindings: false,
    },
    {
      name: 'file with zero findings at all (passes direction)',
      pathArg: 'web/src/workbench/NothingWrongHere.tsx',
      expectFindings: false,
    },
    {
      name: 'sibling directory does not false-match a prefix (passes direction)',
      pathArg: 'web/src/components',
      rule: 'no-such-collision-rule',
      expectFindings: false,
    },
  ];

  const results = cases.map(runSelfTestCase);
  const passed = results.filter(Boolean).length;
  console.log(`\n${passed}/${results.length} self-test cases passed`);

  if (passed !== results.length) {
    process.exit(1);
  }
  process.exit(0);
}

function main() {
  const args = process.argv.slice(2);

  if (args.includes('--self-test')) {
    runSelfTest();
    return;
  }

  const [pathArg, ruleArg] = args;
  if (!pathArg) {
    console.error('Usage: node assert-doctor-clean.mjs <file-or-dir> [rule]');
    console.error('       node assert-doctor-clean.mjs --self-test');
    process.exit(2);
  }

  const pathPrefix = normalizePathArg(pathArg);
  const diagnostics = regenerateDiagnostics();
  const findings = filterFindings(diagnostics, pathPrefix, ruleArg);

  const scopeLabel = `${pathArg}${ruleArg ? ` (${ruleArg})` : ''}`;
  if (findings.length > 0) {
    const message =
      `assert-doctor-clean: ${findings.length} finding(s) under ${scopeLabel}:\n` +
      findings.map((finding) => `  ${formatFinding(finding)}`).join('\n');
    // Throw a real node:assert AssertionError rather than exiting opaquely so
    // callers can classify this as a semantic lint failure.
    assert.fail(message);
  }

  console.log(`assert-doctor-clean: 0 findings under ${scopeLabel}`);
  process.exit(0);
}

main();
