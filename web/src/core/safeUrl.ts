/**
 * The one gate between an untrusted string (an imported cell, a scraped or
 * recorded source URL, a URL a model answer cites) and a URL the DOM may
 * navigate to or load. React does not block `javascript:` in `href`, so any
 * such string reaches `href`/`src` only through these functions; a null result
 * means render the string as text.
 *
 * URLs the app builds itself from ids (`projectBlobUrl`, `routePath`, the
 * server's `/api/...` download and clip paths, `URL.createObjectURL`) are
 * trusted by construction and are not passed through here.
 */

/** `raw` as a link target when it is an absolute http(s) URL, else null. */
export function safeHttpUrl(raw: string | null | undefined): string | null {
  if (typeof raw !== 'string') return null;
  const url = raw.trim();
  if (!/^https?:\/\//i.test(url)) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? url : null;
  } catch {
    return null;
  }
}

// Inline image/audio/video payloads are legitimate media cell values; other
// data: types (text/html, application/*) are not media.
const MEDIA_DATA_URL_RE = /^data:(?:image|audio|video)\/[a-z0-9][a-z0-9.+-]*[;,]/i;

const PROBE_ORIGIN = 'https://app.invalid';

// Host-issued media (preview scratch artifacts) arrives as a same-origin
// `/api/` path. Resolving against a probe origin rejects `//host` and `/\host`
// and normalizes dot segments before the prefix check.
function appApiPath(url: string): string | null {
  if (!url.startsWith('/')) return null;
  try {
    const parsed = new URL(url, PROBE_ORIGIN);
    if (parsed.origin !== PROBE_ORIGIN || !parsed.pathname.startsWith('/api/')) return null;
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return null;
  }
}

/** `raw` as a media resource URL (http(s), an inline image/audio/video data:
 *  URL, or a same-origin `/api/` path), else null. */
export function safeMediaUrl(raw: string): string | null {
  const http = safeHttpUrl(raw);
  if (http) return http;
  const url = raw.trim();
  return MEDIA_DATA_URL_RE.test(url) ? url : appApiPath(url);
}
