import assert from 'node:assert/strict';
import test from 'node:test';
import { redactSetup, setupOutput, startupDiagnostics, classifySetupFailure } from '../src/startup-diagnostics.mjs';

test('installer output waits for complete lines and redacts credentials across chunks', () => {
  const lines = [];
  const output = setupOutput(line => lines.push(line));
  output.write('Downloading Chromium 40%\rAPI_');
  assert.deepEqual(lines, ['Downloading Chromium 40%']);
  output.write('KEY=private');
  assert.equal(lines.length, 1);
  output.write('value\nDone\n');
  assert.deepEqual(lines, ['Downloading Chromium 40%', 'API_KEY=[redacted]', 'Done']);
  assert.equal(redactSetup('https://user:pass@example.com/file'), 'https://[redacted]@example.com/file');
  output.write('token=' + 'x'.repeat(9000));
  output.write('secret remainder\nReady');
  output.end();
  assert.equal(lines.at(-2), '[oversized setup output omitted]');
  assert.equal(lines.at(-1), 'Ready');
  assert.equal(lines.join('').includes('secret remainder'), false);
});


test('one immutable plan tracks actual step durations and freezes only when stopped', () => {
  let clock = 0;
  const log = startupDiagnostics({ version: '1.2.3', platform: 'win32', arch: 'x64', osVersion: '10', now: () => clock });
  log.begin();
  assert.equal(log.snapshot().status, 'checking');
  log.event({ type: 'plan', steps: ['engine', 'components', 'open'], firstTime: true });
  log.event({ type: 'plan', steps: ['open'] });
  log.event({ type: 'step-start', step: 'engine' });
  clock = 1000;
  log.event({ type: 'step-end', step: 'engine', result: 'ok' });
  log.event({ type: 'step-start', step: 'components' });
  clock = 302000;
  assert.equal(log.snapshot().steps[1].elapsedMs, 301000);
  assert.equal(log.snapshot().status, 'running');
  log.event({ type: 'failure', error: Object.assign(new Error('Disk full'), { code: 'ENOSPC' }) });
  assert.equal(log.snapshot().failure.cause, 'disk_space');
  assert.equal(log.snapshot().retryable, false);
  clock = 303000;
  log.event({ type: 'stopped', retryable: true });
  clock = 500000;
  const state = log.snapshot();
  assert.equal(state.elapsedMs, 303000);
  assert.equal(state.retryable, true);
  assert.deepEqual(state.steps.map(step => step.state), ['done', 'stopped', 'next']);
  assert.equal(state.steps[0].elapsedMs, 1000);
  assert.equal(state.firstTime, true);
  assert.match(log.report(), /components: stopped \(301.0s\)/);
  assert.match(log.report(), /Free disk space: not measured/);
  log.begin();
  assert.equal(log.snapshot().status, 'checking');
  assert.equal(log.snapshot().elapsedMs, 0);
  assert.equal(log.snapshot().retryable, false);
  assert.deepEqual(log.snapshot().steps, []);
});

test('diagnostics sanitize paths and credentials before returning snapshots and bound all history to 200 lines', () => {
  const log = startupDiagnostics({ version: '1.2.3', platform: 'darwin', arch: 'arm64', home: '/Users/alice', osVersion: '24' });
  log.begin();
  log.record('/Users/alice/Library/Frisket /home/bob/private /Users/eve/data API_KEY=private');
  log.record('C:\\Users\\bob\\Documents\\private.txt');
  log.record('Authorization: Bearer private');
  log.record('CUSTOM_LOCATION=client-name');
  log.record('/data/client-name/research.csv');
  log.event({ type: 'activity', message: '/Users/alice/cache token=private' });
  const text = JSON.stringify(log.snapshot());
  assert.match(text, /~\/Library\/Frisket/);
  for (const secret of ['alice', 'bob', 'eve', 'private.txt', 'Bearer private', 'token=private', 'client-name']) assert.equal(text.includes(secret), false);
  assert.match(log.snapshot().activity, /~\/cache token=\[redacted\]/);
  const windows = redactSetup('C:\\Users\\alice\\AppData\\Local', 'C:\\Users\\alice');
  assert.equal(windows, '~\\AppData\\Local');
  for (let i = 0; i < 205; i++) log.record(`message ${i}`);
  assert.equal(log.snapshot().logs.length, 200);
  assert.equal(log.snapshot().logs[0].message, 'message 5');
  assert.equal(log.report().includes('message 4\n'), false);
  const copy = log.snapshot();
  copy.logs[0].message = 'mutated';
  assert.equal(log.snapshot().logs[0].message, 'message 5');
});

test('only real measurement fields disclose bytes and unknown failures stay unknown', () => {
  const log = startupDiagnostics({ version: '1', platform: 'linux', arch: 'x64', osVersion: '6' });
  log.begin();
  log.event({ type: 'plan', steps: ['open'] });
  log.event({ type: 'activity', message: 'Downloading', bytes_done: 10 });
  assert.equal(log.snapshot().bytes_done, undefined);
  log.event({ type: 'activity', message: 'Downloading', bytes_done: 10, bytes_total: 20 });
  assert.equal(log.snapshot().bytes_total, 20);
  log.event({ type: 'activity', message: 'Installing components' });
  assert.equal(log.snapshot().bytes_total, undefined);
  log.event({ type: 'failure', error: new Error('installer exit 1'), requiredBytes: NaN, freeDiskBytes: Infinity });
  assert.deepEqual(log.snapshot().failure, { cause: 'unknown' });
  assert.equal(log.snapshot().freeDiskBytes, null);
  log.event({ type: 'failure', cause: 'disk_space', requiredBytes: 100, freeDiskBytes: 30 });
  assert.deepEqual(log.snapshot().failure, { cause: 'disk_space', requiredBytes: 100 });
  assert.equal(log.snapshot().freeDiskBytes, 30);
});

test('failure classification relies on actual cause evidence', () => {
  assert.equal(classifySetupFailure(new Error('installation failed')), 'unknown');
  assert.equal(classifySetupFailure(new Error('not enough information about disk')), 'unknown');
  assert.equal(classifySetupFailure(new Error('failed', { cause: { code: 'ENOSPC' } })), 'disk_space');
  assert.equal(classifySetupFailure({ code: 'EAI_AGAIN' }), 'network');
  assert.equal(classifySetupFailure({ message: 'Could not resolve host: example.com' }), 'network');
  const cycle = {}; cycle.cause = cycle;
  assert.equal(classifySetupFailure(cycle), 'unknown');
});

test('successful one-step opening finishes directly and freezes its elapsed time', () => {
  let clock = 0;
  const log = startupDiagnostics({ version: '1', platform: 'linux', arch: 'x64', now: () => clock });
  log.begin(); log.event({ type: 'plan', steps: ['open'] });
  log.event({ type: 'step-start', step: 'open' });
  clock = 500;
  log.event({ type: 'step-end', step: 'open', result: 'ok' });
  clock = 900;
  assert.equal(log.snapshot().status, 'done');
  assert.equal(log.snapshot().elapsedMs, 500);
});


test('signed diagnostic URLs omit every query and fragment value', () => {
  const log = startupDiagnostics({ version: '1', platform: 'linux', arch: 'x64' });
  log.begin();
  log.record('Download https://example.com/archive.zip?sv=2025&sig=azure-private#fragment-private');
  log.event({ type: 'activity', message: 'https://example.com/file?X-Amz-Signature=aws-private&arbitrary=private#access-private' });
  const text = JSON.stringify(log.snapshot()) + log.report();
  assert.match(text, /https:\/\/example.com\/archive.zip/);
  assert.match(text, /https:\/\/example.com\/file/);
  for (const secret of ['azure-private', 'fragment-private', 'aws-private', 'access-private', 'arbitrary=private', 'sig=', 'X-Amz-Signature']) {
    assert.equal(text.includes(secret), false);
  }
});
