/** Most browser specs exercise returning users; onboarding specs override this. */
export function returningUserStorageState(baseURL: string | undefined) {
  if (!baseURL) throw new Error('Returning-user browser state needs a baseURL.');
  return {
    cookies: [],
    origins: [{
      origin: new URL(baseURL).origin,
      localStorage: [{ name: 'frisket.cloud-welcome.dismissed.v1', value: '1' }],
    }],
  };
}
