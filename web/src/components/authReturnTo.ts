const SIGN_IN_PATH = '/sign-in';
const PENDING_SIGN_IN_KEY = 'frisket:pending-sign-in';
const PENDING_SIGN_IN_TTL_MS = 15 * 60 * 1000;

function safeLocalPath(candidate: string | null, origin: string): string | null {
  if (!candidate || !candidate.startsWith('/') || candidate.startsWith('//')) return null;
  try {
    const parsed = new URL(candidate, origin);
    if (parsed.origin !== origin || parsed.pathname === SIGN_IN_PATH) return null;
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return null;
  }
}

/** Preserve the page that required authentication without accepting an open redirect. */
export function authReturnTo(location: Pick<Location, 'origin' | 'pathname' | 'search' | 'hash'>): string {
  const query = new URLSearchParams(location.search);
  const explicit = safeLocalPath(query.get('returnTo'), location.origin);
  if (explicit) return explicit;
  if (location.pathname !== '/' && location.pathname !== SIGN_IN_PATH) {
    return `${location.pathname}${location.search}${location.hash}`;
  }
  return '/';
}

export function rememberPendingSignIn(
  candidate: string,
  storage?: Storage,
  now = Date.now(),
): void {
  const path = safeLocalPath(candidate, 'https://frisket.local');
  // A callback may render SignIn at root before authentication is complete.
  if (!path || path === '/' || path.startsWith('/?') || path.startsWith('/#')) return;
  try {
    (storage ?? window.localStorage).setItem(PENDING_SIGN_IN_KEY, JSON.stringify({
      path,
      expiresAt: now + PENDING_SIGN_IN_TTL_MS,
    }));
  } catch { /* blocked storage must not block sign-in */ }
}

function pendingSignIn(storage: Storage | undefined, now: number): string | null {
  try {
    const raw = (storage ?? window.localStorage).getItem(PENDING_SIGN_IN_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { path?: unknown; expiresAt?: unknown };
    if (typeof parsed.path !== 'string' || typeof parsed.expiresAt !== 'number'
      || parsed.expiresAt <= now) {
      (storage ?? window.localStorage).removeItem(PENDING_SIGN_IN_KEY);
      return null;
    }
    const path = safeLocalPath(parsed.path, 'https://frisket.local');
    if (!path) (storage ?? window.localStorage).removeItem(PENDING_SIGN_IN_KEY);
    return path;
  } catch {
    try { (storage ?? window.localStorage).removeItem(PENDING_SIGN_IN_KEY); } catch { /* ignored */ }
    return null;
  }
}

function clearPendingSignIn(storage: Storage | undefined): void {
  try { (storage ?? window.localStorage).removeItem(PENDING_SIGN_IN_KEY); } catch { /* ignored */ }
}

/** Consume the same-browser continuation only after the identity probe succeeds. */
export function resumePendingSignIn(
  location: Pick<Location, 'pathname' | 'search' | 'hash'>,
  storage?: Storage,
  now = Date.now(),
): string | null {
  const pending = pendingSignIn(storage, now);
  if (!pending) return null;
  const current = `${location.pathname}${location.search}${location.hash}`;
  clearPendingSignIn(storage);
  if (current === pending) {
    return null;
  }
  if (location.pathname !== '/' && location.pathname !== SIGN_IN_PATH) return null;
  return pending;
}
