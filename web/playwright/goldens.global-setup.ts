// Missing credentials are a LOUD failure, never a silent skip: a golden run
// without a provider key would either mock or green-wash, and both are lies.
// The check lives here rather than at config import so tools that only read
// the config (knip, editors) load it without the key.
export default function requireGoldenCredentials() {
  if (!process.env.GEMINI_API_KEY) {
    throw new Error(
      'GOLDENS NOT RUN: GEMINI_API_KEY is not set. The golden projects make ' +
        'live model calls by design (no mocked model output). Set GEMINI_API_KEY ' +
        '(repo secret in CI, .secrets/frisket.env locally) and re-run.',
    );
  }
}
