import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import test from 'node:test';
import {
  appendBounded, cleanupProved, healthUrl, parseReadyMessage, startBackend, startupMessage,
} from '../src/backend.mjs';

test('readiness parser refuses malformed or non-loopback messages', () => {
  assert.deepEqual(parseReadyMessage({ schema: 1, type: 'ready', host: '127.0.0.1', port: 8000, pid: 22 }), { schema: 1, type: 'ready', host: '127.0.0.1', port: 8000, pid: 22 });
  assert.equal(parseReadyMessage({ schema: 1, type: 'ready', host: 'localhost', port: 8000, pid: 22 }), null);
  assert.equal(parseReadyMessage({ schema: 2, type: 'ready', host: '127.0.0.1', port: 8000, pid: 22 }), null);
  assert.equal(parseReadyMessage({ schema: 1, type: 'ready', host: '127.0.0.1', port: 0, pid: 22 }), null);
});

test('startup message carries an absolute workspace and appends bounded diagnostics', () => {
  const message = JSON.parse(startupMessage('/tmp/workspace', '/tmp/profile', 'secret'));
  assert.deepEqual(message, { schema: 2, workspace: '/tmp/workspace', profile_dir: '/tmp/profile', token: 'secret' });
  assert.throws(() => startupMessage('relative', '/tmp/profile', 'secret'));
  assert.equal(appendBounded('x'.repeat(9000), 'y').length, 8192);
  assert.equal(healthUrl('127.0.0.1', 3000), 'http://127.0.0.1:3000/api/health');
  assert.throws(() => healthUrl('localhost', 3000));
});

test('backend launches through guardian, authenticates health, and waits for guardian close', { skip: process.platform === 'win32' }, async () => {
  let call;
  const child = new EventEmitter();
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdin = { end(value) { child.input = value; } }; child.exitCode = null;
  child.kill = (signal) => { child.signal = signal; child.exitCode = 0; queueMicrotask(() => child.emit('close', 0, null)); return true; };
  const backendPromise = startBackend({
    runtime: { python: '/private/python', env: { PATH: '/private' } }, resourcesPath: '/app/resources', workspace: '/data/workspace', profileDir: '/data/profile', electronPid: 71,
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
  assert.match(child.input, /"profile_dir":"\/data\/profile"/);
  await backend.stop();
  assert.equal(child.signal, 'SIGTERM');
});

test('failed startup waits for guardian acknowledgement before allowing a retry', { skip: process.platform === 'win32' }, async () => {
  const child = new EventEmitter();
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdin = new EventEmitter(); child.stdin.end = () => {};
  child.exitCode = null;
  child.kill = () => { setTimeout(() => { child.exitCode = 0; child.emit('close', 0, null); }, 15); return true; };
  const started = startBackend({
    runtime: { python: '/private/python', env: {} }, resourcesPath: '/app/resources', workspace: '/data/workspace', profileDir: '/data/profile', readyTimeoutMs: 200,
    spawnProcess() { queueMicrotask(() => child.stdout.emit('data', Buffer.from('{"schema":1,"type":"ready","host":"127.0.0.1","port":8123,"pid":9}\n'))); return child; },
    fetchImpl: async () => { throw new Error('health rejected'); },
  });
  await assert.rejects(started, /health rejected/);
  assert.equal(child.exitCode, 0);
});

test('cleanup proof rejects killed guardians and accepts only the shared TERM exemption', () => {
  assert.equal(cleanupProved(0, null), true);
  assert.equal(cleanupProved(null, 'SIGTERM'), true);
  assert.equal(cleanupProved(null, 'SIGKILL'), false);
  assert.equal(cleanupProved(null, 'SIGABRT'), false);
});

test('an already-dead SIGKILL guardian cannot be returned as a ready backend', { skip: process.platform === 'win32' }, async () => {
  const child = new EventEmitter();
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdin = new EventEmitter(); child.stdin.end = () => {};
  child.exitCode = null; child.signalCode = 'SIGKILL'; child.kill = () => true;
  await assert.rejects(startBackend({
    runtime: { python: '/private/python', env: {} }, resourcesPath: '/app/resources', workspace: '/data/workspace', profileDir: '/data/profile',
    spawnProcess() { queueMicrotask(() => child.stdout.emit('data', Buffer.from('{"schema":1,"type":"ready","host":"127.0.0.1","port":8123,"pid":9}\n'))); return child; },
    fetchImpl: async () => new Response(null, { status: 204 }),
  }), /cleanup could not be proven/);
});

test('oversized readiness output is rejected instead of accepting a truncated suffix', { skip: process.platform === 'win32' }, async () => {
  const child = new EventEmitter();
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdin = new EventEmitter(); child.stdin.end = () => {};
  child.exitCode = null;
  child.kill = () => { child.exitCode = 0; queueMicrotask(() => child.emit('close', 0, null)); return true; };
  const started = startBackend({
    runtime: { python: '/private/python', env: {} }, resourcesPath: '/app/resources', workspace: '/data/workspace', profileDir: '/data/profile', readyTimeoutMs: 200,
    spawnProcess() { queueMicrotask(() => child.stdout.emit('data', Buffer.from('x'.repeat(8_193)))); return child; },
    fetchImpl: async () => new Response(null, { status: 204 }),
  });
  await assert.rejects(started, /too much readiness output/);
});


test('slow backend startup remains alive after 20 seconds and reports real readiness', { skip: process.platform === 'win32' }, async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'] });
  const messages = [];
  const child = new EventEmitter();
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdin = { end() {} }; child.exitCode = null;
  let killed = false;
  child.kill = () => { killed = true; child.exitCode = 0; queueMicrotask(() => child.emit('close', 0, null)); };
  const result = startBackend({
    runtime: { python: '/private/python', env: {} }, resourcesPath: '/app/resources', workspace: '/data/workspace', profileDir: '/data/profile',
    spawnProcess: () => child, onActivity: message => messages.push(message),
    fetchImpl: async () => new Response(null, { status: 204 }),
  });
  child.stderr.emit('data', Buffer.from('ordinary server log\nfrisket-startup: Opening workspace databases…\n'));
  assert.equal(messages.at(-1), 'Opening workspace databases…');
  assert.equal(messages.some(line => line.includes('ordinary server log')), false);
  t.mock.timers.tick(21000);
  assert.equal(killed, false);
  assert.match(messages.at(-1), /still starting/);
  child.stdout.emit('data', Buffer.from('{"schema":1,"type":"ready","host":"127.0.0.1","port":8123,"pid":9}\n'));
  const backend = await result;
  assert.match(messages.at(-1), /health check passed/);
  await backend.stop();
});

test('startup deadline reports that downloads finished and preserves failure output', { skip: process.platform === 'win32' }, async () => {
  const messages = [];
  const child = new EventEmitter();
  child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
  child.stdin = { end() {} }; child.exitCode = null;
  child.kill = () => { child.exitCode = 0; queueMicrotask(() => child.emit('close', 0, null)); };
  const keepAlive = setInterval(() => {}, 1000);
  try {
    const result = startBackend({
      runtime: { python: '/private/python', env: {} }, resourcesPath: '/app/resources', workspace: '/data/workspace', profileDir: '/data/profile', readyTimeoutMs: 20,
      spawnProcess: () => child, onActivity: message => messages.push(message),
      fetchImpl: async () => { throw new Error('must not call health before ready'); },
    });
    child.stderr.emit('data', Buffer.from('Importing local server; token=private\n'));
    await assert.rejects(result, /ready signal within 0.02 seconds.*downloads have finished/);
    assert.match(messages.at(-1), /Importing local server; token=\[redacted\]/);
    assert.equal(child.exitCode, 0);
  } finally { clearInterval(keepAlive); }
});


for (const phase of ['readiness', 'health']) {
  test(`cancelling startup during ${phase} waits for child cleanup instead of its deadline`, { skip: process.platform === 'win32' }, async () => {
    const controller = new AbortController();
    const child = new EventEmitter();
    child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
    child.stdin = { end() {} }; child.exitCode = null;
    child.kill = () => { setTimeout(() => { child.exitCode = 0; child.emit('close', 0, null); }, 10); };
    const started = startBackend({
      runtime: { python: '/private/python', env: {} }, resourcesPath: '/app/resources', workspace: '/data/workspace', profileDir: '/data/profile', signal: controller.signal,
      spawnProcess: () => child,
      fetchImpl: async (_url, { signal }) => { controller.abort(); assert.equal(signal.aborted, true); return new Promise(() => {}); },
    });
    if (phase === 'readiness') controller.abort();
    else child.stdout.emit('data', Buffer.from('{"schema":1,"type":"ready","host":"127.0.0.1","port":8123,"pid":9}\n'));
    await assert.rejects(started, { name: 'AbortError' });
    assert.equal(child.exitCode, 0);
  });
}
