/**
 * JSON /auth/request-link and /auth/logout are typed generated calls owned by
 * ../browserAuth. This raw module owns external browser-navigation auth starts.
 */

/** Start one runtime-advertised OIDC provider flow. */
export function oidcSignInUrl(provider: string, returnTo?: string): string {
  const path = `/auth/oidc/${encodeURIComponent(provider)}`;
  return returnTo && returnTo !== '/'
    ? `${path}?${new URLSearchParams({ returnTo }).toString()}`
    : path;
}

export function continueAfterBrowserSignIn(returnTo: string): void {
  window.location.assign(returnTo);
}

/** Start the organization's Google OAuth browser flow. */
export function googleOAuthStartUrl(): string {
  return '/api/org/oauth/google/start';
}
