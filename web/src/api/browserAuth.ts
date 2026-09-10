import { httpContract } from './httpContract';
import { clearTelemetryPseudonym } from '../telemetry/productTelemetry';

class BrowserAuthStatusError extends Error {}

function legacyStatusError(status: number): Error {
  return new Error(`server returned ${status}`);
}

/** Request a passwordless sign-in link through the generated HTTP contract. */
export async function requestMagicLink(email: string): Promise<void> {
  await httpContract(
    'outer.request_link.post',
    {
      pathParams: {},
      query: {},
      body: { email: email.trim() },
      errorFactory: legacyStatusError,
    },
    () => undefined,
  );
}

/** Sign in with a local password through the shared JSON auth route. */
export async function signInWithPassword(email: string, password: string): Promise<void> {
  await httpContract(
    'outer.password_login.post',
    {
      pathParams: {},
      query: {},
      body: { email: email.trim(), password },
      errorFactory: legacyStatusError,
    },
    () => undefined,
  );
}

/** Finish a first magic-link sign-in by creating the identity password. */
export async function completeMagicLink(token: string, password: string): Promise<void> {
  await httpContract(
    'outer.complete_magic_link.post',
    {
      pathParams: {},
      query: { token },
      body: { password },
      errorFactory: legacyStatusError,
    },
    () => undefined,
  );
}

/** Accept a first project invitation and create the identity password. */
export async function completeProjectInvite(token: string, password: string): Promise<void> {
  await httpContract(
    'outer.accept_project_invite.post',
    {
      pathParams: { token },
      query: {},
      body: { password },
      errorFactory: legacyStatusError,
    },
    () => undefined,
  );
}

/** End a browser session without surfacing an expired-session status. */
export async function signOut(): Promise<void> {
  try {
    await httpContract(
      'outer.logout.post',
      {
        pathParams: {},
        query: {},
        errorFactory: (status) => new BrowserAuthStatusError(String(status)),
      },
      () => undefined,
    );
  } catch (error) {
    if (error instanceof BrowserAuthStatusError) return;
    throw error;
  } finally {
    clearTelemetryPseudonym();
  }
}
