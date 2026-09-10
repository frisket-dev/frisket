#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

let min = 95;
for (let i = 2; i < process.argv.length; i += 1) {
  const arg = process.argv[i];
  if (arg === '--min') {
    const next = process.argv[i + 1];
    if (!next) {
      console.error('Missing value for --min');
      process.exit(2);
    }
    min = Number.parseInt(next, 10);
    i += 1;
  } else if (arg.startsWith('--min=')) {
    min = Number.parseInt(arg.slice('--min='.length), 10);
  } else {
    console.error(`Unknown argument: ${arg}`);
    process.exit(2);
  }
}

if (!Number.isInteger(min) || min < 0 || min > 100) {
  console.error(`Invalid --min score: ${min}`);
  process.exit(2);
}

const scriptDir = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(scriptDir, '..');
const binName = process.platform === 'win32' ? 'react-doctor.cmd' : 'react-doctor';
const reactDoctor = resolve(webRoot, 'node_modules', '.bin', binName);

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

const scoreMatch =
  output.match(/\b(\d{1,3})\s*\/\s*100\b/) ??
  output.match(/\b(?:react doctor )?score[:\s]+(\d{1,3})\b/i) ??
  output.match(/^\s*(\d{1,3})\s*$/m);
let scoreText = scoreMatch?.[1];

if (!scoreText) {
  const numbers = [...output.matchAll(/\b(\d{1,3})\b/g)].map((match) => match[1]);
  const uniqueNumbers = [...new Set(numbers)];
  if (uniqueNumbers.length === 1) {
    [scoreText] = uniqueNumbers;
  }
}

if (!scoreText) {
  console.error(`Could not parse React Doctor score from output:\n${output}`);
  process.exit(1);
}

const score = Number.parseInt(scoreText, 10);
console.log(`React Doctor score: ${score}/100 (minimum ${min})`);
if (score < min) {
  process.exit(1);
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

const blockingDiagnostics = diagnostics.filter((diagnostic) => {
  const severity = String(diagnostic?.severity ?? '').toLowerCase();
  const category = String(diagnostic?.category ?? '').toLowerCase();
  return severity === 'error' || severity === 'critical' || category === 'critical';
});

if (blockingDiagnostics.length > 0) {
  console.error(
    `React Doctor reported ${blockingDiagnostics.length} error/critical diagnostics:\n` +
      blockingDiagnostics
        .map((diagnostic) => {
          const location = `${diagnostic.filePath ?? 'unknown'}:${diagnostic.line ?? '?'}`;
          return `- ${diagnostic.severity ?? 'unknown'} ${diagnostic.rule ?? 'unknown'} at ${location}: ${diagnostic.title ?? ''}`;
        })
        .join('\n'),
  );
  process.exit(1);
}

console.log(
  `React Doctor blocking diagnostics: 0 error/critical (${diagnostics.length} total diagnostics)`,
);
