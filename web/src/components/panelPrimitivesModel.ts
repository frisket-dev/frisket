import { ApiError } from '../api/open';

/**
 * Plain (non-component) helpers shared across panel components. Split out of
 * PanelPrimitives.tsx because that file's exports must stay component-only
 * (react-refresh/only-export-components) — pairs with it the way
 * friendlyFiltersPanelModel.ts pairs with FriendlyFiltersPanel.tsx.
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

/**
 * Render a caught value as a user-facing error string: an `ApiError`'s own
 * message, an ordinary `Error`'s message, or `fallback` for anything else
 * (a thrown non-Error, e.g.). Previously copy-pasted between FriendlyFiltersPanel
 * and MentionsPanel with only the fallback string differing per panel.
 */
export function panelErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : fallback;
}
