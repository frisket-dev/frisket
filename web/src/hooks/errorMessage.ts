/**
 * A caught value as user-facing text: an `Error`'s own message (including an
 * empty one), otherwise `fallback`, otherwise the value stringified.
 */
export function errorMessage(error: unknown, fallback?: string): string {
  if (error instanceof Error) return error.message;
  return fallback ?? String(error);
}
