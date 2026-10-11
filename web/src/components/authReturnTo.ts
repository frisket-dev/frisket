const SIGN_IN_PATH = '/sign-in';
const DEVICE_APPROVAL_PATH = '/device';
const PENDING_DEVICE_APPROVAL_KEY = 'frisket:pending-device-approval';
const PENDING_DEVICE_APPROVAL_TTL_MS = 15 * 60 * 1000;

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

function deviceApprovalPath(candidate: string): string | null {
  try {
    const parsed = new URL(candidate, 'https://frisket.local');
    if (parsed.origin !== 'https://frisket.local'
      || parsed.pathname !== DEVICE_APPROVAL_PATH
      || !parsed.searchParams.get('user_code')) return null;
    return `${parsed.pathname}${parsed.search}`;
  } catch {
    return null;
  }
}

export function rememberPendingDeviceApproval(
  candidate: string,
  storage: Storage = window.sessionStorage,
  now = Date.now(),
): void {
  const path = deviceApprovalPath(candidate);
  if (!path) return;
  try {
    storage.setItem(PENDING_DEVICE_APPROVAL_KEY, JSON.stringify({
      path,
      expiresAt: now + PENDING_DEVICE_APPROVAL_TTL_MS,
    }));
  } catch { /* blocked storage must not block sign-in */ }
}

function pendingDeviceApproval(storage: Storage, now: number): string | null {
  try {
    const raw = storage.getItem(PENDING_DEVICE_APPROVAL_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { path?: unknown; expiresAt?: unknown };
    if (typeof parsed.path !== 'string' || typeof parsed.expiresAt !== 'number'
      || parsed.expiresAt <= now) {
      storage.removeItem(PENDING_DEVICE_APPROVAL_KEY);
      return null;
    }
    const path = deviceApprovalPath(parsed.path);
    if (!path) storage.removeItem(PENDING_DEVICE_APPROVAL_KEY);
    return path;
  } catch {
    try { storage.removeItem(PENDING_DEVICE_APPROVAL_KEY); } catch { /* ignored */ }
    return null;
  }
}

function clearPendingDeviceApproval(storage: Storage): void {
  try { storage.removeItem(PENDING_DEVICE_APPROVAL_KEY); } catch { /* ignored */ }
}

/** Consume a pending device continuation only after the identity probe succeeds. */
export function resumePendingDeviceApproval(
  location: Pick<Location, 'pathname' | 'search'>,
  storage: Storage = window.sessionStorage,
  now = Date.now(),
): string | null {
  const pending = pendingDeviceApproval(storage, now);
  if (!pending) return null;
  const current = `${location.pathname}${location.search}`;
  if (current === pending) {
    clearPendingDeviceApproval(storage);
    return null;
  }
  if (location.pathname !== '/' && location.pathname !== SIGN_IN_PATH) return null;
  clearPendingDeviceApproval(storage);
  return pending;
}
