// Pure decision logic for the react-doctor DISPOSITION-TRAIL gate. Metrics
// are smells, not targets — the score is telemetry, never a threshold. What
// actually gates is the disposition trail: every warning is either (a) new
// since the committed baseline (fails, must be fixed or explicitly justified
// in doctor.config.ts), (b) already accounted for (baselined or covered by a
// doctor.config.ts override — passes), or (c) gone (informational, never a
// failure — improvement always passes).
//
// Kept separate from check-react-doctor-gate.mjs (which does the I/O: spawn
// react-doctor, read diagnostics.json/baseline/config off disk) so tests can
// drive this logic against fixture JSON without running the real doctor CLI.

import { readFileSync } from 'node:fs';
import ts from 'typescript';

function isErrorTierDiagnostic(diagnostic) {
  const severity = String(diagnostic?.severity ?? '').toLowerCase();
  const category = String(diagnostic?.category ?? '').toLowerCase();
  return severity === 'error' || severity === 'critical' || category === 'critical';
}

function qualifiedRule(diagnostic) {
  const plugin = diagnostic?.plugin ?? 'unknown';
  const rule = diagnostic?.rule ?? 'unknown';
  return `${plugin}/${rule}`;
}

function pairKey(pair) {
  return `${pair.rule}::${pair.file}`;
}

function sortedUniquePairs(pairs) {
  const byKey = new Map();
  for (const pair of pairs) {
    byKey.set(pairKey(pair), pair);
  }
  return [...byKey.values()].sort((a, b) => pairKey(a).localeCompare(pairKey(b)));
}

// File-level, not line-level: two findings for the same rule in the same
// file collapse to one baseline entry, so line drift alone can't churn the
// baseline.
function dedupeWarningPairs(diagnostics) {
  const pairs = diagnostics
    .filter((diagnostic) => !isErrorTierDiagnostic(diagnostic))
    .map((diagnostic) => ({ rule: qualifiedRule(diagnostic), file: String(diagnostic?.filePath ?? 'unknown') }));
  return sortedUniquePairs(pairs);
}

function collectErrorFindings(diagnostics) {
  return diagnostics.filter(isErrorTierDiagnostic);
}

function parseBaseline(raw) {
  const warnings = Array.isArray(raw?.warnings) ? raw.warnings : [];
  return sortedUniquePairs(
    warnings.map((entry) => ({ rule: String(entry.rule), file: String(entry.file) })),
  );
}

export function loadBaselineFromFile(path) {
  return parseBaseline(JSON.parse(readFileSync(path, 'utf8')));
}

function globToRegExp(glob) {
  const escaped = String(glob)
    .split('*')
    .map((part) => part.replace(/[.+^${}()|[\]\\]/g, '\\$&'))
    .join('[^/]*');
  return new RegExp(`^${escaped}$`);
}

function findDefaultObjectLiteral(sourceFile) {
  let found;
  const visit = (node) => {
    if (found) return;
    if (ts.isExportAssignment(node) && ts.isObjectLiteralExpression(node.expression)) {
      found = node.expression;
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return found;
}

function getObjectProperty(objectLiteral, name) {
  if (!objectLiteral || !ts.isObjectLiteralExpression(objectLiteral)) return undefined;
  for (const prop of objectLiteral.properties) {
    if (!ts.isPropertyAssignment(prop) || !prop.name) continue;
    const propName = ts.isIdentifier(prop.name) || ts.isStringLiteral(prop.name) ? prop.name.text : undefined;
    if (propName === name) return prop.initializer;
  }
  return undefined;
}

function stringArrayLiteralValues(node) {
  if (!node || !ts.isArrayLiteralExpression(node)) return [];
  return node.elements.filter(ts.isStringLiteral).map((element) => element.text);
}

// doctor.config.ts is a plain object literal today (no TS-only syntax), but
// we read it with the real TypeScript AST parser rather than transpiling +
// executing the source (react-doctor's own no-eval rule correctly flags
// eval()/new Function() as running untrusted code strings — this gate
// shouldn't trip its own subject). Walking `export default {...}` down to
// `ignore.overrides[].{files,rules}` string literals is enough for the
// shape doctor.config.ts actually uses.
function parseDoctorConfigOverrides(source) {
  const sourceFile = ts.createSourceFile('doctor.config.ts', source, ts.ScriptTarget.ES2020, true);
  const root = findDefaultObjectLiteral(sourceFile);
  const ignore = getObjectProperty(root, 'ignore');
  const overridesNode = getObjectProperty(ignore, 'overrides');
  const overrides = [];
  if (overridesNode && ts.isArrayLiteralExpression(overridesNode)) {
    for (const element of overridesNode.elements) {
      if (!ts.isObjectLiteralExpression(element)) continue;
      const files = stringArrayLiteralValues(getObjectProperty(element, 'files'));
      const rules = stringArrayLiteralValues(getObjectProperty(element, 'rules'));
      overrides.push({ filePatterns: files.map(globToRegExp), rules: new Set(rules) });
    }
  }
  return overrides;
}

export function loadDoctorConfigOverrides(path) {
  return parseDoctorConfigOverrides(readFileSync(path, 'utf8'));
}

function isCoveredByOverride(pair, overrides) {
  return overrides.some(
    (override) =>
      override.rules.has(pair.rule) &&
      override.filePatterns.some((pattern) => pattern.test(pair.file)),
  );
}

// diagnostics: parsed diagnostics.json array (react-doctor's raw output).
// baseline: array of {rule, file} pairs (already deduped/sorted is fine,
//   parseBaseline/loadBaselineFromFile do that for you).
// overrides: array of {filePatterns, rules} from parseDoctorConfigOverrides.
export function computeGateReport({ diagnostics, baseline, overrides }) {
  const errorFindings = collectErrorFindings(diagnostics);
  const currentWarnings = dedupeWarningPairs(diagnostics);
  const baselineKeys = new Set(baseline.map(pairKey));
  const currentKeys = new Set(currentWarnings.map(pairKey));

  const newWarnings = currentWarnings.filter(
    (pair) => !baselineKeys.has(pairKey(pair)) && !isCoveredByOverride(pair, overrides),
  );
  const disappearedWarnings = baseline.filter((pair) => !currentKeys.has(pairKey(pair)));

  return {
    errorFindings,
    currentWarnings,
    newWarnings,
    disappearedWarnings,
    baselineCount: baseline.length,
    currentCount: currentWarnings.length,
    ok: errorFindings.length === 0 && newWarnings.length === 0,
  };
}
