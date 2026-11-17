/**
 * Plain (non-component) helpers shared across panel components. Split out of
 * PanelPrimitives.tsx because that file's exports must stay component-only
 * (react-refresh/only-export-components).
 */

/**
 * Slugify an arbitrary string into a `data-testid`-safe fragment (non
 * alphanumeric/`_`/`-` runs collapsed to a single `-`, leading/trailing `-`
 * trimmed, capped at 90 chars so a long facet/entity value can't blow out
 * the DOM attribute). Previously copy-pasted verbatim between
 * FriendlyFiltersPanel and MentionsPanel — collapsed here so the two panels'
 * testid derivation can't silently diverge.
 */
export function testIdKey(value: string): string {
  return value.replace(/[^a-zA-Z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 90);
}
