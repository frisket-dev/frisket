import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import test from 'node:test';
import {
  appendBounded, healthUrl, parseReadyMessage, startBackend, startupMessage,
} from '../src/backend.mjs';

test('readiness parser refuses malformed or non-loopback messages', () => {
  assert.deepEqual(parseReadyMessage({ schema: 1, type: 'ready', host: '127.0.0.1', port: 8000, pid: 22 }), { schema: 1, type: 'ready', host: '127.0.0.1', port: 8000, pid: 22 });
  assert.equal(parseReadyMessage({ schema: 1, type: 'ready', host: 'localhost', port: 8000, pid: 22 }), null);
  assert.equal(parseReadyMessage({ schema: 2, type: 'ready', host: '127.0.0.1', port: 8000, pid: 22 }), null);
  assert.equal(parseReadyMessage({ schema: 1, type: 'ready', host: '127.0.0.1', port: 0, pid: 22 }), null);
});

test('startup message carries an absolute workspace and appends bounded diagnostics', () => {
  const message = JSON.parse(startupMessage('/tmp/workspace', 'secret'));
  assert.deepEqual(message, { schema: 1, workspace: '/tmp/workspace', token: 'secret' });
  assert.throws(() => startupMessage('relative', 'secret'));
  assert.equal(appendBounded('x'.repeat(9000), 'y').length, 8192);
  assert.equal(healthUrl('127.0.0.1', 3000), 'http://127.0.0.1:3000/api/health');
  assert.throws(() => healthUrl('localhost', 3000));
});

test('backend launches through guardian, authenticates health, and waits for guardian close', async () => {
  let call;
  const child = new EventEmitter();
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdin = { end(value) { child.input = value; } }; child.exitCode = null;
  child.kill = (signal) => { child.signal = signal; child.exitCode = 0; queueMicrotask(() => child.emit('close', 0, null)); return true; };
  const backendPromise = startBackend({
    runtime: { python: '/private/python', env: { PATH: '/private' } }, resourcesPath: '/app/resources', workspace: '/data/workspace', electronPid: 71,
    spawnProcess(command, args, options) {
      call = { command, args, options };
      queueMicrotask(() => child.stdout.emit('data', Buffer.from('{"schema":1,"type":"ready","host":"127.0.0.1","port":8123,"pid":9}\n')));
      return child;
    },
    fetchImpl: async (url, init) => { assert.equal(url, 'http://127.0.0.1:8123/api/health'); assert.match(init.headers['X-Frisket-Desktop-Token'], /^[a-f0-9]{64}$/); return new Response(null, { status: 204 }); },
  });
  const backend = await backendPromise;
  assert.deepEqual(call.args.slice(0, 6), ['-I', '/app/resources/python/frisket/runtime/_guard.py', '71', '8', '/private/python', '-I']);
  assert.equal(call.options.detached, true);
  assert.match(child.input, /"workspace":"\/data\/workspace"/);
  await backend.stop();
  assert.equal(child.signal, 'SIGTERM');
});
