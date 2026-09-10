/** Return the first value that is already an exact string array.
 * Structured request fields must never pass through delimiter or String()
 * coercion because individual values may themselves contain commas. */
export function firstExactStringArray(...values: unknown[]): string[] | undefined {
  for (const value of values) {
    if (Array.isArray(value) && value.every((item) => typeof item === 'string')) {
      return [...value];
    }
  }
  return undefined;
}
