import assert from 'node:assert/strict';
import test from 'node:test';
import {
  appUrl, authenticatedHeaders, createProtocolHandler, rewriteOwnedRedirect,
} from '../src/protocol.mjs';

test('app URL parsing admits only the exact custom origin', () => {
  assert.equal(appUrl('frisket://app/sheets?tab=one')?.pathname, '/sheets');
  for (const url of [
    'frisket://app:9/', 'frisket://user@app/', 'frisket://app.evil/',
    'https://app/', 'frisket:///path',
  ]) assert.equal(appUrl(url), null, url);
});

test('proxy strips renderer authority and hop-by-hop headers', () => {
  const headers = authenticatedHeaders({
    Host: 'other.example', Connection: 'X-Remove, keep-alive',
    'X-Remove': 'yes', 'X-Frisket-Desktop-Token': 'forged', 'Keep-Alive': '1',
  }, 'a'.repeat(64));
  assert.equal(headers.get('host'), null);
  assert.equal(headers.get('connection'), null);
  assert.equal(headers.get('x-remove'), null);
  assert.equal(headers.get('x-frisket-desktop-token'), 'a'.repeat(64));
});

test('proxy preserves streamed request bodies, range headers, and response status', async () => {
  let seen;
  const handler = createProtocolHandler({
    backend: { host: '127.0.0.1', port: 9123 }, token: 'b'.repeat(64),
    netFetch: async (url, init) => {
      seen = { url, init, body: await new Response(init.body).text() };
      return new Response('partial', { status: 206, headers: { Range: 'bytes=0-6', 'Content-Type': 'text/plain' } });
    },
  });
  const response = await handler(new Request('frisket://app/api/import', {
    method: 'POST', headers: { Range: 'bytes=0-6', 'Content-Type': 'multipart/form-data; boundary=x' }, body: 'content', duplex: 'half',
  }));
  assert.equal(seen.url, 'http://127.0.0.1:9123/api/import');
  assert.equal(seen.body, 'content');
  assert.equal(seen.init.headers.get('range'), 'bytes=0-6');
  assert.equal(seen.init.headers.get('content-type'), 'multipart/form-data; boundary=x');
  assert.equal(response.status, 206);
  assert.equal(await response.text(), 'partial');
});

test('owned redirects return to the app origin while external redirects remain manual', async () => {
  assert.equal(rewriteOwnedRedirect('http://127.0.0.1:8123/login?a=1', { host: '127.0.0.1', port: 8123 }), 'frisket://app/login?a=1');
  assert.equal(rewriteOwnedRedirect('https://example.test/', { host: '127.0.0.1', port: 8123 }), 'https://example.test/');
  const handler = createProtocolHandler({
    backend: { host: '127.0.0.1', port: 8123 }, token: 'c'.repeat(64),
    netFetch: async (_url, init) => new Response(null, { status: 302, headers: { Location: 'http://127.0.0.1:8123/login' } }),
  });
  const response = await handler(new Request('frisket://app/start'));
  assert.equal(response.status, 302);
  assert.equal(response.headers.get('location'), 'frisket://app/login');
});

test('HTML responses keep upstream CSP and add the desktop policy', async () => {
  const handler = createProtocolHandler({
    backend: { host: '127.0.0.1', port: 8123 }, token: 'd'.repeat(64),
    netFetch: async () => new Response('<main/>', {
      headers: { 'Content-Type': 'text/html', 'Content-Security-Policy': "img-src 'none'" },
    }),
  });
  const response = await handler(new Request('frisket://app/'));
  const csp = response.headers.get('content-security-policy');
  assert.match(csp, /img-src 'none'/);
  assert.match(csp, /script-src 'self'/);
});
