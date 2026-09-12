/** @typedef {{ host: string, port: number }} DesktopBackend */

export const APP_SCHEME = 'frisket';
export const APP_HOST = 'app';
export const APP_ORIGIN = `${APP_SCHEME}://${APP_HOST}`;

const HOP_BY_HOP_HEADERS = new Set([
  'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
  'te', 'trailer', 'transfer-encoding', 'upgrade',
]);

/**
 * Parse a URL that is allowed to enter the desktop application origin.
 *
 * Node intentionally reports a null `origin` for custom schemes, so each
 * component is checked instead of relying on URL.origin.
 * @param {string} value
 * @returns {URL | null}
 */
export function appUrl(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (
    url.protocol !== `${APP_SCHEME}:` || url.hostname !== APP_HOST ||
    url.port !== '' || url.username !== '' || url.password !== ''
  ) return null;
  return url;
}

/** @param {string} value @param {DesktopBackend} backend */
export function ownedLoopbackUrl(value, backend) {
  let url;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (
    (url.protocol !== 'http:' && url.protocol !== 'https:') ||
    url.hostname !== '127.0.0.1' || url.port !== String(backend.port) ||
    url.username !== '' || url.password !== ''
  ) return null;
  return url;
}

/** @param {URL} appRequest @param {DesktopBackend} backend */
export function backendUrl(appRequest, backend) {
  return `http://127.0.0.1:${backend.port}${appRequest.pathname}${appRequest.search}`;
}

/**
 * Make the only request headers that may cross the renderer/backend boundary.
 * @param {Headers | Record<string, string>} supplied
 * @param {string} token
 */
export function authenticatedHeaders(supplied, token) {
  const headers = new Headers(supplied);
  const connectionTokens = (headers.get('connection') || '').split(',')
    .map((name) => name.trim().toLowerCase()).filter(Boolean);
  for (const name of HOP_BY_HOP_HEADERS) headers.delete(name);
  for (const name of connectionTokens) headers.delete(name);
  headers.delete('host');
  headers.delete('x-frisket-desktop-token');
  headers.set('X-Frisket-Desktop-Token', token);
  return headers;
}

export function desktopCsp() {
  return "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data: http: https:; media-src 'self' blob: data: http: https:; font-src 'self' data:; worker-src 'self' blob:; connect-src 'self' https:; object-src 'none'; base-uri 'none'; frame-ancestors 'self'";
}

/** @param {string | null} location @param {DesktopBackend} backend */
export function rewriteOwnedRedirect(location, backend) {
  if (!location) return location;
  const url = ownedLoopbackUrl(location, backend);
  return url ? `${APP_ORIGIN}${url.pathname}${url.search}${url.hash}` : location;
}

/**
 * Build the privileged proxy handler.  `netFetch` is injected to allow tests
 * to exercise the public boundary without starting Electron.
 * @param {{ backend: DesktopBackend, token: string, netFetch: typeof fetch }} options
 */
export function createProtocolHandler({ backend, token, netFetch }) {
  if (!Number.isInteger(backend?.port) || backend.port < 1 || backend.port > 65535) {
    throw new Error('invalid desktop backend');
  }
  if (typeof token !== 'string' || token.length < 32) throw new Error('invalid desktop token');

  return async function handle(request) {
    const incoming = appUrl(request.url);
    if (!incoming) return new Response('Not found', { status: 404 });
    const method = String(request.method || 'GET').toUpperCase();
    const init = {
      method,
      headers: authenticatedHeaders(request.headers || {}, token),
      redirect: 'manual',
    };
    if (method !== 'GET' && method !== 'HEAD' && request.body) {
      init.body = request.body;
      // Chromium's Request body is a stream.  Undici requires this marker;
      // Electron's net.fetch accepts it and keeps multipart boundaries intact.
      init.duplex = 'half';
    }
    const upstream = await netFetch(backendUrl(incoming, backend), init);
    const headers = new Headers(upstream.headers);
    headers.delete('connection');
    headers.delete('keep-alive');
    headers.delete('transfer-encoding');
    headers.delete('upgrade');
    const location = rewriteOwnedRedirect(headers.get('location'), backend);
    if (location) headers.set('location', location);
    if ((headers.get('content-type') || '').toLowerCase().includes('text/html')) {
      // A second CSP is an additional policy: browsers enforce it alongside
      // an upstream policy rather than replacing that policy with ours.
      headers.append('content-security-policy', desktopCsp());
    }
    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers,
    });
  };
}

/** @param {{ protocol: { handle: Function, unhandle: Function, isProtocolHandled: Function }, net: { fetch: typeof fetch } }} electron @param {DesktopBackend & { token: string }} backend */
export function installProtocol(electron, backend) {
  if (electron.protocol.isProtocolHandled(APP_SCHEME)) electron.protocol.unhandle(APP_SCHEME);
  return electron.protocol.handle(APP_SCHEME, createProtocolHandler({
    backend,
    token: backend.token,
    netFetch: electron.net.fetch.bind(electron.net),
  }));
}
