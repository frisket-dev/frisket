import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { redactSetup, setupOutput, startupDiagnostics } from '../src/startup-diagnostics.mjs';

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

test('diagnostics preserve attempt timings, versions and bounded history', () => {
  let clock = 0;
  const log = startupDiagnostics({ version: '1.2.3', platform: 'win32', arch: 'x64', now: () => clock });
  log.begin();
  clock = 21000;
  log.record('Waiting for local server');
  log.begin();
  assert.match(log.report(), /1.2.3 \(win32\/x64\)/);
  assert.match(log.report(), /\[21.0s\] Waiting for local server/);
  assert.match(log.report(), /Startup attempt 2/);
  for (let i = 0; i < 100; i++) log.record(`message ${i}`);
  assert.equal(log.report().split('\n').length, 82);
});

test('startup screen shows real activity and elapsed silence, using text rather than HTML', async () => {
  const nodes = Object.fromEntries(['#status', '#progress', '.setup-progress', '#activity', '#elapsed', '#setup-log'].map(key => [key, { textContent: '', style: { setProperty() {} }, setAttribute() {} }]));
  let tick;
  let clock = 0;
  const context = { window: {}, Date: { now: () => clock }, setInterval: fn => { tick = fn; }, document: { body: { dataset: {} }, querySelector: selector => nodes[selector], querySelectorAll: () => [] } };
  vm.runInNewContext(await readFile(new URL('../ui/startup.js', import.meta.url), 'utf8'), context);
  context.window.updateStartupActivity('[2.0s] Downloading Chromium 40%');
  assert.equal(nodes['#activity'].textContent, 'Downloading Chromium 40%');
  clock = 15000;
  tick();
  assert.match(nodes['#elapsed'].textContent, /15s elapsed.*No new output for 15s/);
  context.window.updateStartupActivity('[17.0s] <b>Finished</b>');
  assert.equal(nodes['#activity'].textContent, '<b>Finished</b>');
  assert.match(nodes['#setup-log'].textContent, /Downloading Chromium 40%/);
});
