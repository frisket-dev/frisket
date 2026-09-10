/**
 * JSON /auth/request-link and /auth/logout are typed generated calls owned by
 * ../browserAuth. This raw module owns external browser-navigation auth starts.
 */

/** Start one runtime-advertised OIDC provider flow. */
export function oidcSignInUrl(provider: string): string {
  return `/auth/oidc/${encodeURIComponent(provider)}`;
}

/** Start the organization's Google OAuth browser flow. */
export function googleOAuthStartUrl(): string {
  return '/api/org/oauth/google/start';
}
